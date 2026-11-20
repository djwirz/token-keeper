# 19. Observability as built: where alarm email lives, what each metric counts, how alarms are forced

Date: 2026-11-20
Status: Accepted

## Context
ADR 0009 chose Powertools, EMF metrics, X-Ray and three alarms to SNS email. Slice 6 builds them, and several choices it left open are expensive to change once alarms exist and someone relies on them:

- **The alarm email address.** Like the budget's, it must not be in the repo or in GitHub. But CI deploys `TokenKeeper`, where the alarms live (ADR 0016).
- **What `RefreshFailed` counts.** It drives an alarm. Counting transient token-endpoint errors would page on a GitHub outage, which ADR 0015 says should raise no alarms.
- **Forged GitHub deliveries.** Spec §7 says "a metric is incremented" for forged connect links, callbacks and GitHub webhooks, but spec §8's list has only `ConnectRejected`.
- **Tracing.** The X-Ray SDKs, which Powertools' Tracer is built on, have been in maintenance mode since February 2026, and AWS ends their support in February 2027. The X-Ray service itself carries on.
- **How each alarm is forced** (spec §8) without leaving the demo broken or drifted from its template. Spec §8's DLQ method, pointing the deliverer at a URL that returns 500 with a short retry window, means editing the shared receiver-URL parameter (which `/connect` also reads) and shortening a day-long retry in code.

## Options considered
For the email:
1. A hand-deployed `Alerts` stack that owns the topic and its subscription, like `Budget`. `TokenKeeper` builds the topic's ARN from its fixed name.
2. Put the topic in the `Budget` stack. One fewer stack, but the budget stack would hold more than the budget.
3. `TokenKeeper` owns the topic, and the subscription resolves the address from SSM at deploy time. All in one template, but CI's deploy would then manage Dan's subscription.

For tracing: Lambda active tracing only; Powertools' Tracer as well; or cut X-Ray (spec §3's second cut).

For the DLQ drill: spec §8's 500 URL; or a poison message on the sync queue.

## Decision
- **Email: option 1.** The `Alerts` stack (topic `token-keeper-alarms`, one email subscription) exists in the CDK app only when `ALERT_EMAIL` is set, and is deployed by hand with the legacy synthesizer. CI never sees the address. `TokenKeeper`'s alarms point at `arn:…:sns:us-east-1:${AWS::AccountId}:token-keeper-alarms`, so there's no export and no account literal. Every alarm notifies on ALARM *and* on OK, so the email that something was put back arrives as well. ADR 0016 is unchanged.
- **Metrics:** eleven, in namespace `TokenKeeper`, with one dimension, `service`. Names live in `token_keeper.metrics`, and the CDK dashboard and alarms import them.
  - `RefreshFailed` counts only a refresh GitHub **rejected** (`bad_refresh_token`). Transient token-endpoint errors stay out. A lost rotation still alarms, because its next attempt is rejected. A refresh token past its stored expiry makes no call, so it isn't counted here; it shows in `SyncFailed` and the logs.
  - `SyncFailed` counts every sync that ended without a new snapshot: transient, `needs_reauth`, or an unexpected error (so a poison message counts once per receive). A skipped sync or a lost lease counts nothing.
  - `DeliveryLatency` is the milliseconds from the message's SQS `SentTimestamp` to the 2xx, so retries are included: it's how long the main app waited.
  - `ConnectRejected` counts rejected connect links and rejected callbacks (missing or unknown state, missing code). A write conflict is a race, not a rejection.
  - **`GitHubWebhookRejected`** is new: the 401s and 400s on `/github/webhook`. Counting them under `ConnectRejected` would mislead whoever reads the dashboard.
  - `RevocationReceived` counts every verified, well-formed revocation, whether it was acted on, repeated or for an unknown user.
- **Tracing: Lambda active tracing only** (`tracing=ACTIVE`), with no SDK. Each Lambda gets its own segment, and SQS-triggered invocations show as linked traces. Spec §8's "connect → callback" trace was never possible: the browser goes to GitHub between the two requests, so they are two traces. HTTP APIs don't emit X-Ray segments. The `correlation_id` in logs still ties a whole flow together.
- **Alarm shapes:**
  - DLQ: the larger of the two DLQs' visible messages, per five minutes, ≥ 1.
  - RefreshFailed: ≥ 1 in any of twelve five-minute periods, so it fires within minutes rather than at the end of an hour.
  - PlannerMissing: three empty fifteen-minute periods of the planner's `Invocations`, with missing data breaching.
- **Forcing, with nothing changed in any template:**
  - DLQ: a non-JSON message on the sync queue. The worker fails it three times and SQS moves it to the sync DLQ, so the real redrive policy is what fires the alarm. Then a valid delivery message is put on the delivery DLQ and moved back with `start-message-move-task`, which proves the runbook's redrive.
  - RefreshFailed: a synthetic account (`scripts/account_ops.py create-synthetic`) with a fake refresh token. GitHub rejects it. The account is then deleted with the runbook's own procedure.
  - PlannerMissing: disable the schedule by hand, then put it back with the same definition and check it against the template.

## Consequences
- **Cost:** this should round to nothing. CloudWatch's always-free tier covers ten custom metrics, ten alarm metrics and three dashboards, and custom metrics are prorated by the hour they're emitted. Only `AccountsDue` is emitted around the clock (every planner run, zero included); the others appear a few hours a month at most. The alarms use four metrics, since the DLQ alarm reads two. X-Ray's free tier (100,000 traces a month) is far above demo volume. Email notifications are free at this volume. Even outside the free tier, it's well under a dollar a month.
- **No IAM change** for metrics: EMF goes through the log stream. Active tracing adds `xray:PutTraceSegments` and `xray:PutTelemetryRecords` to each Lambda role, and the exact-grants test says so.
- **A one-time hand step:** Dan deploys `Alerts` and clicks the confirmation link. Until then, alarms change state but email no one. An "unsubscribe" click in any alarm email silently ends notifications. The runbook says how to check.
- **Spec §8 changes** in this PR: the DLQ forcing method, the traces line, `RefreshFailed`'s definition, and `GitHubWebhookRejected` in the metric list.
- **The OpenTelemetry route stays open.** If deeper traces are wanted later, ADOT's Lambda layer can replace active tracing without touching handler code.
