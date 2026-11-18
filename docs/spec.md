# Token Keeper: spec

Status: Draft for PR #1 · Date: 2026-11-18 · Owner: Dan

## 1. Problem

Apps that act on a user's third-party account (GitHub, Instagram, Google…) all need the same unglamorous machinery: run the OAuth consent, store tokens safely, refresh them before they expire, notice when the user revokes access, and tell the product what state each connection is in. When that machinery lives inside the main product, it tends to be bolted on: tokens sit in plaintext columns, refreshes race each other, and "why did this account stop syncing?" has no answer.

Token Keeper is a small connector service that owns all of that for one provider, GitHub. Users connect through it. It keeps their tokens alive, uses them on a schedule to sync a small snapshot of their GitHub data, and reports each account's state and snapshot to a **main app** through one signed webhook endpoint. Tokens never leave Token Keeper.

**Who the main app is.** For v1 it's a tiny **demo receiver** in this repo. It verifies webhook signatures, stores the latest event per account, and renders a status page: avatar, login, state and recent activity. Its only job is to make the contract real end to end and give the README something to show.

**What a viewer sees.** Someone clicks "Connect GitHub" on the receiver page and approves the GitHub App. Their profile and recent public activity appear. A few hours later a scheduled sync updates the activity on its own. If they revoke the app on GitHub, the card flips to "needs re-auth" at the next sync.

### Why this is worth building (portfolio framing)
It shows AWS serverless (EventBridge Scheduler → SQS → Lambda, DynamoDB), OAuth done carefully (PKCE, state, single-use refresh-token rotation), envelope-free field encryption with KMS, infrastructure as code (CDK, Python), CI/CD with OIDC, and observability. It also mirrors, using public API knowledge only, the long-lived-token refresh pattern of Instagram's platform. See [ADR 0002](adr/0002-github-app-with-expiring-user-tokens.md).

## 2. Non-goals (v1)

- **A second provider.** The code keeps a narrow `Provider` interface so one could be added later, but only GitHub ships.
- **Token vending or proxying.** No API hands a token to the main app or makes GitHub calls on its behalf. See [ADR 0008](adr/0008-sync-snapshots-not-token-vending.md).
- **Real multi-tenancy.** Every record and event carries an `app_id`, but exactly one main app is registered. There's no tenant onboarding and no per-tenant isolation tests.
- **An admin dashboard in Token Keeper.** Operators use CloudWatch. The only UI is the connect/callback pages and the demo receiver's status page.
- **Instant revocation.** GitHub's `github_app_authorization` webhook is a stretch goal. In v1, revocation is detected at the next sync.
- **Multiple environments.** One deployed environment, `demo`.

## 3. Appetite

- **Time:** one to two focused weeks. If it runs over, cut scope (stretch goals first, then dashboard polish), not quality gates.
- **Money:** a $5/month AWS budget with alerts at 50% and 100%, created **before** the first deploy. The one fixed cost expected is the customer-managed KMS key, about a dollar a month. Everything else should round to nothing at demo volume.
- **Shape:** about six to eight thin PRs after this one (see §12).

## 4. Architecture

```
                 ┌───────────────── Token Keeper (AWS) ─────────────────────────┐
 Demo receiver   │                                                               │
 "Connect" link ─┼─▶ API GW ─▶ connect λ ──(state+PKCE)──▶ GitHub authorize      │
 (signed by      │             │                                                 │
  main app)      │   GitHub ───┼─▶ API GW ─▶ callback λ ─▶ exchange code ─▶ KMS ─▶ DynamoDB
                 │                                                       │       │
                 │   EventBridge Scheduler (every 15 min)                │       │
                 │        └─▶ planner λ ─ query due accounts (GSI) ──────┘       │
                 │                 └─▶ SQS sync-queue (+DLQ)                     │
                 │                         └─▶ worker λ: lease → refresh? →      │
                 │                             sync GitHub → write state         │
                 │                                 └─▶ SQS delivery-queue (+DLQ) │
                 │                                       └─▶ deliverer λ ────────┼─▶ POST signed webhook
                 └───────────────────────────────────────────────────────────────┘        │
                                                                                          ▼
                                                                      Demo receiver λ + table + status page
```

### Components
| Component | Role |
|---|---|
| `connect` λ | Validates the signed connect link, creates an OAuth `state` record (with PKCE verifier, TTL'd), and redirects to GitHub. |
| `callback` λ | Checks `state`, exchanges the code (with the PKCE verifier), fetches `/user`, encrypts tokens, upserts the account, enqueues an immediate sync, and redirects back to the receiver. |
| `planner` λ | Runs every 15 minutes from EventBridge Scheduler. Queries accounts whose `next_due_at` has passed and sends one SQS message per account. If too many recent calls to GitHub have failed, it skips the run (see §7). |
| `worker` λ | Consumes `sync-queue`. Takes a lease on the account, refreshes if the access token is near expiry, fetches the snapshot, applies the state machine, bumps `version`, writes, and enqueues a delivery. |
| `deliverer` λ | Consumes `delivery-queue`. Signs and POSTs the event, and retries with backoff through SQS visibility. Exhausted retries go to the DLQ. |
| Demo receiver | A separate CDK stack: an API route, a Lambda, a small table, and an HTML status page. It also builds signed connect links for its users. |

Rationale and alternatives: [ADR 0004](adr/0004-planner-sqs-worker-schedule.md).

### Sync cadence
- Normal: every **6 hours** per active account, with jitter so accounts spread out.
- If the access token expires in under 10 minutes when a sync runs, the worker refreshes first. With an 8-hour token and a 6-hour cadence, most syncs use a still-valid token and roughly every other sync refreshes. That keeps the refresh token rotating far inside its 6-month life.
- `refresh_failing` accounts are retried sooner, with exponential backoff capped at about an hour.
- Demo knob: a config value can shorten the cadence (e.g. 15 minutes) for recording the GIF.

### Data model (DynamoDB, single table, on-demand)
| Item | Key | Notable attributes |
|---|---|---|
| Account | `PK=ACCOUNT#<account_id>` `SK=META` | `app_id`, `external_user_id`, `github_user_id`, `login`, `state`, `version`, `next_due_at`, `access_token_ct`, `refresh_token_ct`, `access_expires_at`, `refresh_expires_at`, `lease_until`, `lease_owner`, `last_error_code`, `consecutive_failures` |
| Snapshot | `PK=ACCOUNT#<id>` `SK=SNAPSHOT` | profile fields, last ~10 public events, `fetched_at` |
| OAuth state | `PK=STATE#<random>` | `app_id`, `external_user_id`, `pkce_verifier_ct`, `return_to`, TTL of about 10 minutes |
| Link index | `PK=LINK#<app_id>#<external_user_id>` | `account_id` (one GitHub account per main-app user in v1) |

GSI `due`: partition `state`, sort `next_due_at`. The planner queries `active` and `refresh_failing` for `next_due_at <= now`. At demo scale a partition per state is fine. A bucketed key would be the change if it ever wasn't (an open question, not a v1 task).

`*_ct` fields are KMS ciphertext ([ADR 0003](adr/0003-field-level-kms-encryption.md)).

## 5. Provider: GitHub App with expiring user tokens

Decision and Instagram comparison: [ADR 0002](adr/0002-github-app-with-expiring-user-tokens.md).

- **Registration:** one GitHub App, owned by Dan's personal account. User-to-server token expiry is **on**, and the app has **no repository or account permissions**. Public profile and public events need none. Webhooks are off in v1.
- **Token facts** (checked against GitHub docs on 2026-11-18): the access token lasts 8 hours and the refresh token 6 months. **Refresh tokens are single-use**: a refresh returns a new pair and invalidates the old access and refresh tokens.
- **Endpoints used:** `GET /user`, and `GET /users/{login}/events/public` (public only, so nothing private ever reaches a public page), plus `POST https://github.com/login/oauth/access_token` for the code exchange and refresh.
- **The rotation hazard:** if two workers refresh the same account at once, one of them persists a pair GitHub has already invalidated, and the account is lost until the user re-auths. The worker therefore holds a DynamoDB **lease** (a conditional write on `lease_until < now`) for the whole refresh → persist step. It writes the new pair before doing anything else with it.

## 6. Interfaces

### 6.1 Main app → Token Keeper: signed connect link
`GET /connect?app_id=…&external_user_id=…&return_to=…&exp=…&sig=…`

- `sig` = hex HMAC-SHA256 over the canonical query string (every parameter except `sig`, sorted), using the **connect-link secret**. It's a separate secret from the webhook one ([ADR 0007](adr/0007-signed-connect-links-state-pkce.md)).
- The link is rejected if it's expired (`exp` more than 10 minutes out, or already past), the signature is bad, `app_id` is unknown, or `return_to` isn't on that app's allow-list.
- Rejections render a plain error page and log a reason code, never the parameters.

### 6.2 GitHub → Token Keeper: OAuth callback
`GET /callback?code=…&state=…`. The `state` record is single-use (deleted on read). An error from GitHub (e.g. `access_denied`) redirects to `return_to` with `?result=denied`.

### 6.3 Token Keeper → main app: signed webhook (the one endpoint)
This follows the [Standard Webhooks](https://www.standardwebhooks.com/) spec ([ADR 0005](adr/0005-standard-webhooks-full-state-events.md)).

Headers:
- `webhook-id`: a unique event id (ULID). The receiver dedupes on it.
- `webhook-timestamp`: unix seconds. The receiver rejects anything more than 5 minutes off.
- `webhook-signature`: `v1,<base64 HMAC-SHA256(secret, id + "." + timestamp + "." + body)>`. It may hold several space-separated signatures during secret rotation.

Body: always the **full current state**, never a delta.
```json
{
  "type": "account.updated",
  "app_id": "demo",
  "account_id": "acc_01J…",
  "external_user_id": "u_123",
  "version": 42,
  "state": "active",
  "state_reason": null,
  "provider": "github",
  "github": { "user_id": 1234, "login": "octocat" },
  "snapshot": {
    "fetched_at": "2026-11-18T20:00:00Z",
    "profile": { "name": "…", "avatar_url": "…", "public_repos": 8, "followers": 3 },
    "recent_events": [ { "type": "PushEvent", "repo": "octocat/hello", "created_at": "…" } ]
  },
  "occurred_at": "2026-11-18T20:00:01Z"
}
```
- **Event types:** `account.connected`, `account.updated` (snapshot or state change), `account.disconnected`.
- **Ordering:** none guaranteed. The receiver keeps the highest `version` per account and ignores lower ones.
- **Delivery:** a 2xx means delivered. Anything else, or a timeout (5 seconds), is retried with exponential backoff for about a day, then sent to the DLQ and an alarm fires. Because every event is full state, the next successful delivery heals any gap.
- **Never in a payload:** tokens, token expiry times, or any field marked secret.

## 7. Account state machine and failure modes

```
          connect ok                       refresh/sync transient error
 (none) ───────────▶ active ◀──────────────────────────┐
                       │  ▲ success                    │
                       │  └──────────── refresh_failing ┘
                       │ 401 / bad_refresh_token /           │ refresh token expired,
                       │ revoked                             │ or failing > ~24h
                       ▼                                     ▼
                  needs_reauth ◀────────────────────────────┘
                       │ user reconnects → active
                       ▼ user/operator disconnect (any state)
                  disconnected  (tokens deleted)
```

| Failure | Detected by | Behaviour |
|---|---|---|
| Access token expired | `access_expires_at`, or a 401 on the API call | Refresh under lease, then retry the call once. |
| Refresh token rejected (`bad_refresh_token`) or past `refresh_expires_at` | Token endpoint response | → `needs_reauth`. Tokens deleted. Event sent. |
| User revoked the app | 401 on refresh or API call | → `needs_reauth` at the next sync (within about 6 hours). |
| Rate limited (403/429 with rate-limit headers) | Response headers | Account goes to `refresh_failing`, and `next_due_at` is set to the reset or `retry-after` time. No retry storm. |
| GitHub 5xx or timeout | Response or exception | `refresh_failing`, with backoff. After about a day of continuous failure → `needs_reauth`. |
| GitHub broadly down | The share of failed calls to GitHub across recent runs is high | The planner skips enqueueing and logs `planner.skipped_provider_unhealthy`, so a provider outage doesn't mark everyone broken. |
| Concurrent refresh | Lease conditional write fails | The second worker drops the message (or re-enqueues it with a delay). It never touches tokens. |
| Worker crash mid-refresh after GitHub rotated | Lease expires; the stored pair is now invalid | → `needs_reauth` on the next attempt. Documented as a known residual risk; the window is the few milliseconds between GitHub's response and the conditional write. |
| Receiver down | Deliverer non-2xx | Retries for about a day, then the DLQ. State in Token Keeper is unaffected. |
| Poison message | The same message fails 5 times | DLQ, plus an alarm. |
| Malformed or forged connect link / callback | Signature or state check | Rejected. A metric is incremented. Nothing stored. |

## 8. Observability

Powertools for AWS Lambda (Python): structured JSON logs, EMF metrics and X-Ray traces ([ADR 0009](adr/0009-observability-powertools.md)).

- **Logs:** every line carries `account_id`, `app_id`, `webhook_id` where relevant, and `correlation_id` (passed through SQS message attributes). There's a log scrubber, and a test asserts that no token-shaped string (`ghu_`, `ghr_`) ever reaches a log line.
- **Metrics:** `RefreshSucceeded`, `RefreshFailed{reason}`, `SyncSucceeded`, `SyncFailed{reason}`, `DeliverySucceeded`, `DeliveryFailed`, `DeliveryLatency`, `AccountsByState{state}` (from the planner), `PlannerSkipped`, `ConnectRejected{reason}`.
- **Traces:** connect → callback, and planner → SQS → worker → SQS → deliverer.
- **Dashboard:** one CloudWatch dashboard covering the metrics above, queue depths and Lambda errors.
- **Alarms (SNS → email):**
  1. Either DLQ has any message.
  2. Refresh failure rate is elevated over an hour.
  3. The planner hasn't run in 45 minutes (missing-data alarm on an invocation metric).
- **Runbook:** `docs/runbook.md` gets one section per alarm: what it means, how to check, and how to recover (DLQ redrive).
- **Budget:** an AWS Budget of $5/month, alerting by email at 50% and 100%, created by CDK and deployed first.

## 9. Security

- **Tokens at rest:** field-level KMS encryption with a customer-managed key, using `account_id` as encryption context. Only the `callback` and `worker` roles have `kms:Decrypt`. DynamoDB encryption sits underneath ([ADR 0003](adr/0003-field-level-kms-encryption.md)).
- **Tokens in transit and in use:** held only in Lambda memory. Never logged, never put in an SQS message (messages carry `account_id` only), never sent in a webhook.
- **Secrets** (GitHub App client secret, connect-link secret, webhook signing secret): SSM Parameter Store SecureString, read at cold start, never in the repo, CI logs or PR text. Webhook secret rotation uses Standard Webhooks multi-signature.
- **OAuth:** a random 32-byte `state` (single-use, TTL'd) plus PKCE S256 ([ADR 0007](adr/0007-signed-connect-links-state-pkce.md)). `return_to` is checked against an allow-list, so there are no open redirects.
- **IAM:** least privilege through CDK grants, one role per Lambda. The deliverer can't read tokens. The planner can only query the GSI and send to SQS.
- **CI to AWS:** GitHub Actions OIDC. The deploy role is scoped to this repo's `main` branch and the `demo` environment. No AWS keys are stored anywhere.
- **API edge:** API Gateway HTTP API with route throttling, which limits brute force against `/connect` and `/callback`.
- **Data minimisation:** public profile and public events only, a GitHub App with no permissions, and snapshots overwritten rather than accumulated. On `disconnected`, tokens and the snapshot are deleted.
- **Repo hygiene:** a secret scanner (gitleaks) in CI. Synthetic or Dan's own account data only.

## 10. Infrastructure as code, testing and CI

- **IaC:** AWS CDK in Python, with two stacks, `TokenKeeper` and `DemoReceiver`, plus a tiny `Bootstrap`/`Budget` stack deployed first ([ADR 0006](adr/0006-aws-cdk-python-for-iac.md)).
- **Layout:** `src/token_keeper/` for the app (Python 3.12), `infra/` for CDK, `receiver/` for the demo receiver, and `tests/`.
- **Tests:**
  - Unit: state machine transitions, signing and verification against the Standard Webhooks published test vectors, connect-link signing, the token-scrubbing logger.
  - Integration (local): moto for DynamoDB, SQS and KMS, and recorded GitHub responses (respx or similar) for the token and API endpoints, including `bad_refresh_token`, 401, 403 rate-limit and 5xx.
  - Concurrency: two workers race on one account against moto DynamoDB. Exactly one refresh call happens, and the stored pair is the one GitHub returned last.
  - Infra: CDK assertion tests (KMS decrypt only granted to the two roles, DLQs attached, alarms exist, budget exists).
- **CI (GitHub Actions):** on every PR, `ruff`, `mypy`, `pytest`, `cdk synth`, `cdk diff` (posted to the PR via OIDC read-only role), and gitleaks. On merge to `main`, `cdk deploy` behind a GitHub `demo` environment with required approval.

## 11. Acceptance criteria (v1 "done", depth 3)

1. **Live demo:** deployed in `demo`. Dan connects their GitHub account from the receiver page and sees profile and recent public activity. A scheduled sync visibly updates it. Revoking the app on GitHub flips the card to `needs_reauth` at the next sync. A GIF of this is in the README.
2. **Refresh proven end to end:** logs and metrics in the deployed environment show at least one real refresh-token rotation. The concurrency test proves two workers can't spend one refresh token.
3. **Tests, CI and OIDC deploy:** every test category in §10 runs in CI on each PR. Deploys come only from `main` through OIDC with approval, and no AWS keys exist in GitHub.
4. **Ops:** the dashboard is live, all three alarms are wired to email (each tested once by forcing the condition), the $5 budget alarm existed before the first app deploy, and `docs/runbook.md` covers each alarm.
5. **Security checks:** the token-in-logs test passes, a DynamoDB item dump shows only ciphertext for tokens, and gitleaks is clean.
6. **Record:** ADRs for every expensive decision, and each PR description says how AI was used and what it got wrong.

Stretch (not required): the GitHub `github_app_authorization` revocation webhook, an admin "force re-verify", and a second provider.

## 12. Proposed slice plan (one PR each, after this spec)

1. Repo skeleton, CI (lint, type, test, gitleaks), CDK app with the Budget stack and OIDC deploy role. **Budget deployed.**
2. The webhook contract library: sign and verify with test vectors, plus the demo receiver (verify, store, status page), deployed.
3. Connect → callback: GitHub App, signed links, state and PKCE, encrypted token storage. `account.connected` delivered through the deliverer queue.
4. Planner → SQS → worker sync with snapshot, with no refresh yet (tokens stay valid for 8 hours).
5. Refresh under lease, plus the concurrency test, plus the state machine and failure handling.
6. Observability: Powertools, dashboard, alarms, runbook.
7. Demo polish: cadence knob, README GIF, case-study notes.

## 13. Open questions

1. **Receiver page visibility.** It shows only public GitHub data, but should it be public at all, or behind a simple shared password? (Leaning public, read-only.)
2. **One GitHub account per main-app user?** v1 says yes, and a reconnect replaces the existing link. Is that right for the demo?
3. **Provider-down threshold.** What failure share and window should make the planner skip a run? To be set from real data after slice 4, not guessed now.
4. **Lost-rotation recovery.** Is there any way to recover from the crash-after-rotation window short of re-auth? (Believed not; worth one more doc check.)
5. **GSI hot partition.** Only matters far beyond demo scale. Note it in the case study as a known scaling limit, or bucket the key now?
6. **Roadmap wording.** The build-plan roadmap says "Terraform". With [ADR 0006](adr/0006-aws-cdk-python-for-iac.md), that becomes "IaC (CDK, Python)". The roadmap needs updating outside this repo.
7. **Case-study angle.** How much of the Instagram comparison goes in the README versus only in ADR 0002?
