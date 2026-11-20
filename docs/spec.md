# Token Keeper: spec

Status: Draft for PR #1 · Date: 2026-11-18 · Owner: Dan

## 1. Problem

Apps that act on a user's third-party account (GitHub, Instagram, Google…) all need the same unglamorous machinery: run the OAuth consent, store tokens safely, refresh them before they expire, notice when the user revokes access, and tell the product what state each connection is in. When that machinery lives inside the main product, it tends to be bolted on: tokens sit in plaintext columns, refreshes race each other, and "why did this account stop syncing?" has no answer.

Token Keeper is a small connector service that owns all of that for one provider, GitHub. Users connect through it. It keeps their tokens alive, uses them on a schedule to sync a small snapshot of their GitHub data, and reports each account's state and snapshot to a **main app** through one signed webhook endpoint. Tokens never leave Token Keeper.

**Who the main app is.** For v1 it's a tiny **demo receiver** in this repo. It verifies webhook signatures, stores the latest event per account, and renders a status page: avatar, login, state, last-synced time and recent activity. Its only job is to make the contract real end to end and give the README something to show.

**What a viewer sees.** The status page is public and read-only. Dan unlocks the "Connect GitHub" button with a demo passcode and approves the GitHub App. Their profile and recent public activity appear. Later, a scheduled sync updates the "last synced" time (and the activity, once GitHub's events feed has caught up). Then Dan revokes the app in GitHub's settings and, within seconds, the card flips to "needs re-auth". That sequence is the README GIF.

### Why this is worth building (portfolio framing)
It shows AWS serverless (EventBridge Scheduler → SQS → Lambda, DynamoDB), OAuth done carefully (PKCE, state, single-use refresh-token rotation under a lease), field-level KMS encryption, a signed inbound webhook from GitHub, infrastructure as code (CDK, Python), CI/CD with OIDC, and observability. It also mirrors, using public API knowledge only, the long-lived-token refresh pattern of Instagram's platform. See [ADR 0002](adr/0002-github-app-with-expiring-user-tokens.md).

## 2. Non-goals (v1)

- **A second provider.** The code keeps a narrow `Provider` interface so one could be added later, but only GitHub ships.
- **Token vending or proxying.** No API hands a token to the main app or makes GitHub calls on its behalf. See [ADR 0008](adr/0008-sync-snapshots-not-token-vending.md).
- **Real multi-tenancy.** Every record and event carries an `app_id`, but exactly one main app is registered, from config. There's no tenant onboarding and no per-tenant isolation tests.
- **A disconnect API.** A user leaves by revoking the app on GitHub (§6.4). Deleting an account's data outright is an operator step in the runbook. See [ADR 0011](adr/0011-account-identity-and-contract-states.md).
- **Surviving long GitHub outages gracefully.** Transient errors are retried with capped backoff and never change an account's state. There's no circuit breaker or provider-health logic.
- **Open sign-up on the demo.** Only the passcode holder can connect an account; visitors watch (§6.1).
- **An admin dashboard in Token Keeper.** Operators use CloudWatch. The only UI is the connect/callback pages and the demo receiver's status page.
- **Multiple environments.** One deployed environment, `demo`.

## 3. Appetite

- **Time:** one to two focused weeks. Two is the honest estimate.
- **Money:** a $5/month AWS budget with alerts at 50% and 100%, created **before** the first deploy. Expected costs are the customer-managed KMS key (about a dollar a month) and a handful of custom CloudWatch metrics (low single dollars at worst, see §8). Everything else should round to nothing at demo volume.
- **Shape:** seven thin PRs after this one, local-first: the first three never touch AWS (see §12).
- **If time runs short, cut in this order.** The project exists to prove a few things: the refresh lifecycle survives concurrency, tokens are safe, it's deployed and operable, and the GIF shows it working. Cut what proves none of those first:
  1. `cdk diff` posted to PRs (CI still runs `cdk synth`).
  2. X-Ray tracing (logs with correlation ids and metrics stay).
  3. The dashboard beyond the alarms' own metrics.
  4. Receiver page styling.

  Never cut: the lease and its concurrency test, KMS encryption, the token-in-logs test, the budget, OIDC deploys, the revocation flip, and the three alarms with their runbook.

## 4. Architecture

```
                 ┌───────────────── Token Keeper (AWS) ─────────────────────────┐
 Demo receiver   │                                                               │
 "Connect" link ─┼─▶ API GW ─▶ connect λ ──(state+PKCE)──▶ GitHub authorize      │
 (signed by      │             │                                                 │
  main app)      │   GitHub ───┼─▶ API GW ─▶ callback λ ─▶ exchange code ─▶ KMS ─▶ DynamoDB
                 │   GitHub ───┼─▶ API GW ─▶ github-webhook λ (revocations) ───▶ │
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
| `callback` λ | Checks `state`, exchanges the code (with the PKCE verifier), fetches `/user`, encrypts tokens, writes the account (§4 "Writes and the lease") with `next_due_at` about 30 minutes out (the planner is only the fallback if the immediate sync is lost), stores a profile-only snapshot, enqueues `account.connected` and an immediate sync, and redirects back to the receiver. |
| `github-webhook` λ | Receives GitHub App webhooks. Verifies `X-Hub-Signature-256`, handles `github_app_authorization` (the user revoked the app): looks up the account by GitHub user id, moves it to `needs_reauth`, deletes its tokens, and enqueues `account.updated`. Ignores other events. See [ADR 0010](adr/0010-github-revocation-webhook.md). |
| `planner` λ | Runs every 15 minutes from EventBridge Scheduler. Queries `active` accounts whose `next_due_at` has passed, pushes each one's `next_due_at` about 30 minutes ahead (so the next run doesn't enqueue it again), and sends one SQS message per account. |
| `worker` λ | Consumes `sync-queue`. Takes a lease on the account, refreshes if the access token is near expiry, fetches the snapshot, applies the state machine, bumps `version`, writes, and enqueues a delivery. |
| `deliverer` λ | Consumes `delivery-queue`. Reads the account's current state, builds and signs the event, and POSTs it. Retries with backoff through SQS visibility. Exhausted retries go to the DLQ. |
| Demo receiver | A separate CDK stack: an API route, a Lambda, a small table, and an HTML status page. It also builds signed connect links for its one demo user, behind a passcode. |

Rationale and alternatives: [ADR 0004](adr/0004-planner-sqs-worker-schedule.md).

### Sync cadence
- Normal: every **6 hours** per active account, with jitter so accounts spread out.
- If the access token expires in under 10 minutes when a sync runs, the worker refreshes first. With an 8-hour token and a 6-hour cadence, about half of syncs find a still-valid token and the other half refresh. That keeps the refresh token rotating far inside its 6-month life.
- After a transient error, the account stays `active` and `next_due_at` backs off 15, 30, then 60 minutes, capped there (or set to the rate-limit reset time). Details: [ADR 0015](adr/0015-sync-worker-failures-and-lease-budget.md).
- Demo knob: a config value can shorten the cadence (e.g. 15 minutes) for recording the GIF.

### Writes and the lease
Every change to an Account item is a conditional write that bumps `version`, the planner's push and the lease included, so versions can skip numbers. There are two kinds of writer:

- **The worker** takes a lease with a conditional update: `(attribute_not_exists(lease_until) OR lease_until < :now) AND state = active`, setting `lease_owner` and `lease_until` (60 seconds), with `ReturnValues=ALL_NEW`. It uses **only the tokens returned by that update**, never anything it read before, and re-checks expiry against them. A refreshed pair is written (conditional on `lease_owner = :me`) before it's used. Its final write is conditional on `lease_owner = :me`, and clears the lease. Its Lambda timeout (40 seconds) is below the lease, and it starts no token call with under 20 seconds of lease left ([ADR 0015](adr/0015-sync-worker-failures-and-lease-budget.md)).
- **Everyone else** (the callback on reconnect, the github-webhook on revocation) takes over: their write clears `lease_owner` and `lease_until` unconditionally and bumps `version`. A worker that was mid-sync then fails its final write, discards whatever it holds (including a freshly rotated pair, which belongs to a grant that's been replaced or revoked), and logs `lease_lost`.

### Data model (DynamoDB, single table, on-demand)
| Item | Key | Notable attributes |
|---|---|---|
| Account | `PK=ACCOUNT#<account_id>` `SK=META` | `app_id`, `external_user_id`, `github_user_id`, `login`, `state`, `state_reason`, `version`, `next_due_at`, `access_token_ct`, `refresh_token_ct`, `access_expires_at`, `refresh_expires_at`, `lease_until`, `lease_owner`, `last_error_code`, `consecutive_failures` |
| Snapshot | `PK=ACCOUNT#<id>` `SK=SNAPSHOT` | `snapshot`: profile fields, last ~10 public events, `fetched_at` (one JSON string) |
| OAuth state | `PK=STATE#<sha256 of state>` `SK=META` | `app_id`, `external_user_id`, `pkce_verifier_ct`, `return_to`, `correlation_id`, `ttl` of about 10 minutes |
| User link | `PK=LINK#<app_id>#<external_user_id>` `SK=META` | `account_id` |
| GitHub link | `PK=GHUSER#<github_user_id>` `SK=META` | `account_id` (used by revocation webhooks) |

Times are Numbers (epoch seconds) and `*_ct` fields are Binary. The state item is keyed by the state's hash, so someone who can read the table can't replay a live state. Layout and types: [ADR 0013](adr/0013-token-keeper-table-layout.md).

The callback writes Account, User link, GitHub link and the profile-only Snapshot in one transaction. Identity rules are in [ADR 0011](adr/0011-account-identity-and-contract-states.md).

GSI `due`: partition `state`, sort `next_due_at`, keys-only projection (no ciphertext in the index). The planner queries only `active` with `next_due_at <= now`; `needs_reauth` accounts have no `next_due_at`, so they drop out of the index. One partition per state is a known scaling limit far beyond demo scale, noted for the case study rather than fixed.

`*_ct` fields are KMS ciphertext ([ADR 0003](adr/0003-field-level-kms-encryption.md)).

## 5. Provider: GitHub App with expiring user tokens

Decision and Instagram comparison: [ADR 0002](adr/0002-github-app-with-expiring-user-tokens.md).

- **Registration:** one GitHub App, owned by Dan's personal account. User-to-server token expiry is **on**, and the app has **no repository or account permissions**. Public profile and public events need none. Webhooks are **on**, pointed at `/github/webhook` with a secret; a GitHub App receives `github_app_authorization` by default.
- **Token facts** (checked against GitHub docs on 2026-11-18): the access token (`ghu_…`) lasts 8 hours and the refresh token (`ghr_…`) 6 months. **Refresh tokens are single-use**: once used, that refresh token and the old access token stop working. GitHub documents no grace period.
- **Endpoints used:** `GET /user` (every sync; its `login` is the one used next), and `GET /users/{login}/events/public` (public only, so nothing private ever reaches a public page), plus `POST https://github.com/login/oauth/access_token` for the code exchange and refresh. Token-endpoint calls send `Accept: application/json` and treat an `error` field in the body as the failure signal, whatever the HTTP status.
- **Events feed caveats:** GitHub only returns the last 30 days of public events and says delivery can lag from seconds to hours. The demo account needs recent public activity, and the receiver shows `fetched_at` so a sync is visible even when no new event has arrived.
- **The rotation hazard:** if two workers refresh the same account at once, or one refreshes with a stale refresh token, GitHub rejects it and the account looks revoked. The lease rules in §4 prevent both. The worker writes the new pair before doing anything else with it.

## 6. Interfaces

### 6.1 Main app → Token Keeper: signed connect link
`GET /connect?app_id=…&external_user_id=…&return_to=…&exp=…&sig=…`

- **Canonical string:** every parameter except `sig`, sorted by name, each written `name=value` with RFC 3986 percent-encoding (unreserved characters left as is, space as `%20`, uppercase hex), joined with `&`.
- `sig` = lowercase hex HMAC-SHA256 of that string, using the **connect-link secret**. It's a separate secret from the webhook one ([ADR 0007](adr/0007-signed-connect-links-state-pkce.md)). Compared in constant time. Test vectors live in the repo, shared by the signer and the verifier.
- The link is rejected if it's expired (`exp` more than 10 minutes out, or already past), the signature is bad, `app_id` is unknown, or `return_to` isn't on that app's allow-list. The allow-list matches scheme, host, port and path exactly; `return_to` may not carry a query or fragment.
- Rejections render a plain error page and log a reason code, never the parameters. Connect pages send `Referrer-Policy: no-referrer`.
- **Demo receiver:** it signs links for a single demo user, and only after the visitor enters the demo passcode (an SSM parameter, generated at random rather than chosen, since route throttling slows guessing but doesn't stop it). The status page itself needs no passcode.

### 6.2 GitHub → Token Keeper: OAuth callback
`GET /callback?code=…&state=…`. The `state` record is single-use (deleted on read). An error from GitHub (e.g. `access_denied`) with a valid `state` redirects to `return_to` with `?result=denied`; a missing or unknown `state` renders the error page. Success redirects with `?result=connected`. If the GitHub account is already linked to a different main-app user, the callback stores nothing and redirects with `?result=already_linked`.

### 6.3 Token Keeper → main app: signed webhook (the one endpoint)
This follows the [Standard Webhooks](https://www.standardwebhooks.com/) spec ([ADR 0005](adr/0005-standard-webhooks-full-state-events.md)).

Headers:
- `webhook-id`: a unique event id (ULID), created when the event is enqueued and kept across retries. The receiver logs it but doesn't dedupe on it: `version` does that (see Ordering, and [ADR 0012](adr/0012-receiver-dedupes-on-version.md)).
- `webhook-timestamp`: unix seconds of this delivery attempt. The receiver rejects anything more than 5 minutes off.
- `webhook-signature`: `v1,<base64 HMAC-SHA256(secret, id + "." + timestamp + "." + body)>`, computed per attempt. It may hold several space-separated signatures during secret rotation.

Body: always the **full current state**, read when the event is sent (not when it was enqueued), never a delta.
```json
{
  "type": "account.updated",
  "app_id": "demo",
  "account_id": "acc_01J…",
  "external_user_id": "u_demo",
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
- **Event types:** `account.connected` (every successful callback, including reconnects) and `account.updated` (every successful sync, and every state change). At connect time the snapshot holds the profile and an empty `recent_events`.
- **States:** `active` or `needs_reauth`. `state_reason` is `null` when active, and otherwise one of `revoked` (GitHub told us), `refresh_rejected` (`bad_refresh_token`), `refresh_expired`, or `unauthorized` (a 401 that a refresh didn't fix). Transient errors are not part of the contract. See [ADR 0011](adr/0011-account-identity-and-contract-states.md).
- **Ordering and duplicates:** no ordering is guaranteed. The receiver keeps the highest `version` per account and ignores equal or lower ones, which also makes redeliveries no-ops. Versions increase but can skip numbers: internal bookkeeping writes bump them too.
- **Delivery:** a 2xx means delivered. Anything else, or a timeout (5 seconds), is retried with exponential backoff for about a day, then sent to the DLQ and an alarm fires. For an `active` account, the next sync's event heals a gap. A `needs_reauth` account isn't synced again, so a lost event for it stays lost until the DLQ is redriven (a runbook step).
- **Never in a payload:** tokens, token expiry times, or any field marked secret.

### 6.4 GitHub → Token Keeper: App webhook
`POST /github/webhook`. Verified with `X-Hub-Signature-256` (hex HMAC-SHA256 of the raw body with the GitHub App webhook secret, constant-time compare). Only `github_app_authorization` is acted on: the `sender.id` is looked up through the GitHub link item, and the account moves to `needs_reauth` with reason `revoked`. Unknown users and other event types get a 2xx and a log line. Handling is idempotent, so GitHub's redeliveries are harmless. See [ADR 0010](adr/0010-github-revocation-webhook.md).

## 7. Account state machine and failure modes

```
          connect ok
 (none) ───────────▶ active ◀──── transient error: stays active, backs off
                       │  ▲
   revoked webhook /   │  │ user reconnects
   bad_refresh_token / │  │
   refresh expired /   ▼  │
   401 after refresh  needs_reauth  (tokens deleted)
```

| Failure | Detected by | Behaviour |
|---|---|---|
| Access token near expiry | `access_expires_at` (re-checked after taking the lease) | Refresh under lease, persist, then sync. |
| 401 on an API call | Response | Refresh once under lease and retry the call. If the refresh is rejected or the retry also gets 401 → `needs_reauth` (`refresh_rejected` or `unauthorized`). A sync refreshes at most once: a 401 right after a near-expiry refresh in the same sync → `needs_reauth` (`unauthorized`), since a token GitHub has just issued being refused means the grant is gone. |
| Refresh token rejected (`bad_refresh_token`) | `error` field in the token-endpoint body | The refresh token came from the lease write, so it's current: → `needs_reauth` (`refresh_rejected`). Tokens deleted. Event sent. |
| Refresh token past `refresh_expires_at` | Stored expiry | → `needs_reauth` (`refresh_expired`) without calling GitHub. |
| User revoked the app | `github_app_authorization` webhook (seconds); otherwise the next sync's 401 | → `needs_reauth` (`revoked` or `unauthorized`). Tokens deleted. Event sent. |
| Rate limited (403/429 with rate-limit headers) | Response headers | Stays `active`. `next_due_at` is set to the reset or `retry-after` time. No event. |
| GitHub 5xx, timeout or token-endpoint error other than `bad_refresh_token` | Response or exception | Stays `active`. `last_error_code` and `consecutive_failures` recorded, backoff capped at about an hour. No event. |
| Concurrent sync | Lease conditional write fails | The second worker drops the message. The account's `next_due_at` brings it back on a later planner run. It never touches tokens. |
| Reconnect or revocation during a sync | Worker's final write fails (`lease_owner` changed) | The worker discards what it holds and logs `lease_lost`. The takeover write already holds the correct state. |
| Lost rotation | GitHub rotated the pair but we never stored it: a timeout or lost response from the token endpoint, or a crash before the write | → `needs_reauth` on the next attempt. GitHub documents no grace period, so re-auth is the only recovery. Accepted as a residual risk; a lost response is the likeliest cause. |
| Receiver down | Deliverer non-2xx | Retries for about a day, then the DLQ. State in Token Keeper is unaffected. |
| Poison message | The same message keeps failing | Retried for about a day like any failed delivery, then the DLQ, plus an alarm. |
| Malformed or forged connect link, callback or GitHub webhook | Signature or state check | Rejected. A metric is incremented. Nothing stored. |

## 8. Observability

Powertools for AWS Lambda (Python): structured JSON logs, EMF metrics and X-Ray traces ([ADR 0009](adr/0009-observability-powertools.md)).

- **Logs:** every line carries `account_id`, `app_id`, `webhook_id` where relevant, and `correlation_id` (passed through SQS message attributes). Failure reasons go in logs, not metric dimensions. There's a log scrubber, and a test asserts that no token-shaped string (`ghu_`, `ghr_`) ever reaches a log line. Log groups keep two weeks.
- **Metrics** (one `service` dimension, no others; Powertools' cold-start metric off): `RefreshSucceeded`, `RefreshFailed`, `SyncSucceeded`, `SyncFailed`, `DeliverySucceeded`, `DeliveryFailed`, `DeliveryLatency`, `AccountsDue` (from the planner), `ConnectRejected`, `RevocationReceived`. That's about ten custom metrics; most are only emitted a few hours a day, so the cost stays at a dollar or two at worst.
- **Traces:** connect → callback, and each Lambda's own segment. SQS hops show up as linked traces, not one continuous trace; the `correlation_id` in logs ties a whole flow together.
- **Dashboard:** one CloudWatch dashboard covering the metrics above, queue depths and Lambda errors.
- **Alarms (SNS → email):**
  1. **DLQ:** either DLQ has one or more visible messages. Forced by pointing the deliverer at a URL that returns 500 with a short retry window.
  2. **Refresh failure:** `RefreshFailed` sums to one or more in an hour. Forced with a synthetic account whose stored refresh token is invalid.
  3. **Planner missing:** the planner's `Invocations` metric is absent for 45 minutes (missing data treated as breaching). Forced by disabling the schedule.
- **Runbook:** `docs/runbook.md` gets one section per alarm (what it means, how to check, how to recover, including DLQ redrive), plus how to delete an account's data on request.
- **Budget:** an AWS Budget of $5/month, alerting by email at 50% and 100%, created by CDK and deployed first.

## 9. Security

- **Tokens at rest:** field-level KMS encryption with a customer-managed key, using `account_id` as encryption context. Within the stack, only the `callback` and `worker` roles have `kms:Decrypt`. The key keeps CDK's default key policy, so the account's administrators can still decrypt; a key policy that denies them is left out on purpose at demo scale. DynamoDB encryption sits underneath ([ADR 0003](adr/0003-field-level-kms-encryption.md)).
- **Tokens in transit and in use:** held only in Lambda memory. Never logged, never put in an SQS message (messages carry `account_id` only), never sent in a webhook.
- **Secrets** (GitHub App client secret, GitHub App webhook secret, connect-link secret, outbound webhook signing secret, demo passcode): SSM Parameter Store SecureString, read at cold start, never in the repo, CI logs or PR text. Outbound webhook secret rotation uses Standard Webhooks multi-signature.
- **OAuth:** a random 32-byte `state` (single-use, TTL'd) plus PKCE S256 ([ADR 0007](adr/0007-signed-connect-links-state-pkce.md)). `return_to` is checked against an exact-match allow-list, so there are no open redirects.
- **Inbound GitHub webhooks:** signature checked before parsing; the handler can only move an account to `needs_reauth` and delete its tokens, so a forged request that somehow passed could do no more than force a re-auth.
- **IAM:** least privilege through CDK grants, one role per Lambda. The deliverer and github-webhook roles can't decrypt tokens. The planner can only query the GSI, update Account items, emit metrics and send to SQS; a `dynamodb:Attributes` condition limits its updates to the attributes its code names (`next_due_at`, `version`, and the `state` its condition reads). Roles that touch one kind of item carry a `dynamodb:LeadingKeys` condition on that key prefix. Both were proven in the first deploy ([ADR 0017](adr/0017-dynamodb-condition-keys.md)). Only the callback and worker roles have `kms:Decrypt`.
- **CI to AWS:** GitHub Actions OIDC. When a job uses an environment, GitHub's OIDC subject is `<prefix>:environment:demo` and carries no branch, so the deploy role trusts that subject and the `demo` environment's deployment-branch rule allows only `main`. A separate read-only role trusts `<prefix>:pull_request` for `cdk diff`. The repo uses GitHub's immutable subjects, so `<prefix>` names the owner and repo by name and numeric ID (`repo:<owner>@<owner_id>/token-keeper@<repo_id>`). Both roles can only assume CDK's bootstrap roles, and they live in a `CiRoles` stack that, like `Budget`, is deployed by hand and never by CI ([ADR 0016](adr/0016-ci-deploys-through-oidc-and-bootstrap-roles.md)). No AWS keys are stored anywhere.
- **API edge:** API Gateway HTTP API with route throttling, which limits brute force against `/connect`, `/callback` and the passcode.
- **Data minimisation:** public profile and public events only, a GitHub App with no permissions, and snapshots overwritten rather than accumulated. On `needs_reauth`, tokens are deleted; the public snapshot stays so the card still shows who needs to reconnect.
- **Repo hygiene:** a secret scanner (gitleaks) in CI. Synthetic or Dan's own account data only.

## 10. Infrastructure as code, testing and CI

- **IaC:** AWS CDK in Python, with two stacks, `TokenKeeper` and `DemoReceiver`, plus a tiny `Budget` stack deployed first and a `CiRoles` stack for the OIDC roles ([ADR 0006](adr/0006-aws-cdk-python-for-iac.md), [ADR 0016](adr/0016-ci-deploys-through-oidc-and-bootstrap-roles.md)). Templates are environment-agnostic apart from the region, so no account ID is written into them. Operator steps: `docs/deploy.md`.
- **Layout:** `src/token_keeper/` for the app (Python 3.12), `infra/` for CDK, `receiver/` for the demo receiver, `local/` for the local runner, and `tests/`.
- **Data that must survive any deploy:** Token Keeper's DynamoDB table and the KMS key are set to `RETAIN`. (The demo receiver's table is disposable and is destroyed with its stack.) A table replacement would lose every token, and a deleted key would make every stored token unreadable. CDK assertion tests check both.

### Local development
There are two places this runs: a laptop and the one deployed `demo` environment. Handlers are plain Python functions, and most of what the project has to prove is Python logic, so it's built and proven locally first.

- **Local runner:** a small script that maps the HTTP routes to the handlers and drains the queues in-process, against moto in server mode (DynamoDB, SQS, KMS). No LocalStack or SAM local: they cost setup time and still don't prove IAM, so a deploy is needed anyway.
- **GitHub:** the GitHub App lists a `localhost` callback URL alongside the deployed one, so the real OAuth flow, real refreshes and real rotations all work locally against Dan's account. Revocation webhooks are tested with recorded deliveries, or live through smee.io (GitHub's webhook proxy for app development).
- **What only a deploy proves:** IAM grants actually working, API Gateway throttling, the Scheduler and SQS → Lambda wiring (retries, DLQ redrive), real KMS encryption context, metrics and alarms, X-Ray, and OIDC deploys. These are configuration, written in CDK alongside each slice, checked by `cdk synth` and assertion tests in CI, and proven once in the first deploy.
- **Tests:**
  - Unit: state machine transitions, signing and verification against the Standard Webhooks published test vectors, connect-link signing against the repo's own test vectors, GitHub webhook signature checks, the token-scrubbing logger.
  - Integration (local): moto for DynamoDB, SQS and KMS, and recorded GitHub responses (respx or similar) for the token and API endpoints, including `bad_refresh_token`, 401, 403 rate-limit, 5xx and a `github_app_authorization` delivery.
  - Concurrency, against moto DynamoDB:
    - Two workers race on one account: exactly one makes a refresh call, the stored pair equals that call's response, and the losing worker makes no token call.
    - Stale read: a worker that read the account before another worker's refresh still uses the current refresh token once it holds the lease.
    - Takeover: a revocation lands mid-sync; the worker's final write fails and the account stays `needs_reauth` with no tokens.
  - Infra: CDK assertion tests (KMS decrypt only granted to the two roles, DLQs attached, alarms exist, budget exists, table and key retained).
- **CI (GitHub Actions):** on every PR, `ruff`, `mypy`, `pytest`, `cdk synth`, `cdk diff` (posted to the PR via the read-only OIDC role, from slice 4 on, once there's a deployed stack to diff against), and gitleaks. On merge to `main`, `cdk deploy` of `DemoReceiver` and `TokenKeeper` through the GitHub `demo` environment with required approval.

## 11. Acceptance criteria (v1 "done", depth 3)

1. **Live demo:** deployed in `demo`. Dan connects their GitHub account from the receiver page and sees profile and recent public activity. A scheduled sync visibly updates the "last synced" time. Revoking the app in GitHub's settings flips the card to "needs re-auth" within a minute, and reconnecting flips it back. A GIF of this is in the README.
2. **Refresh proven end to end:** logs and metrics in the deployed environment show at least one real refresh-token rotation. The three concurrency tests in §10 pass.
3. **Tests, CI and OIDC deploy:** every test category in §10 runs in CI on each PR. Deploys come only from `main` through OIDC with approval, and no AWS keys exist in GitHub. The exceptions are the hand-deployed `Budget` and `CiRoles` stacks, and slice 4's first deploy of the app stacks, made before the deploy workflow existed on `main` ([ADR 0016](adr/0016-ci-deploys-through-oidc-and-bootstrap-roles.md)).
4. **Ops:** the dashboard is live, all three alarms are wired to email and each has been fired once by the forcing method in §8, the $5 budget alarm existed before the first app deploy, and `docs/runbook.md` covers each alarm.
5. **Security checks:** the token-in-logs test passes, a scripted DynamoDB scan shows only ciphertext in `*_ct` fields and no `ghu_`/`ghr_` string anywhere, and gitleaks is clean.
6. **Record:** ADRs for every expensive decision, and each PR description says how AI was used and what it got wrong.

Stretch (not required): an admin "force re-verify" and a second provider.

## 12. Proposed slice plan (one PR each, after this spec)

Slices 1–3 are local only. Each one also adds the CDK code for what it builds, checked by `cdk synth` and assertion tests in CI, so the first deploy ships infrastructure that has already been reviewed.

1. **Local foundation:** repo skeleton, CI (lint, type, test, gitleaks, `cdk synth`), the webhook contract library (sign and verify with test vectors), connect-link signing with test vectors, the demo receiver running locally (verify, store, status page, passcode-gated connect), and the local runner.
2. **Connect → callback, locally:** GitHub App (localhost callback), signed links, state and PKCE, encrypted token storage on moto, Powertools logger with the token scrubber and its test. `account.connected` delivered to the local receiver through the deliverer queue. (The callback's immediate sync enqueue came in slice 3, with the sync queue.)
3. **Sync and refresh, locally:** planner → queue → worker with snapshot, refresh under lease, the state machine and failure handling, and the three concurrency tests. Real refresh-token rotations against Dan's GitHub account. The store's takeover write for revocation lands here too (the concurrency test needs it); slice 5 calls it from the webhook.
4. **First deploy:** the Budget stack first, then the OIDC deploy roles, then `DemoReceiver` and `TokenKeeper` with everything built so far (no second receiver deploy: URLs go through SSM, ADR 0014). Fix what real IAM, Scheduler and SQS wiring turn up. Prove or drop the IAM narrowing put off until now. Ends with a deployed connect, a deployed sync and one real refresh.
5. **Revocation webhook:** `github-webhook` λ, tested locally with recorded deliveries, then live in `demo` with the instant flip to `needs_reauth`.
6. **Observability:** metrics, dashboard, alarms (each forced once), runbook.
7. **Demo polish:** cadence knob, README GIF, case-study notes.

The trade-off: IAM and wiring surprises all land in slice 4 instead of spread across slices. With nothing live and one user, that's accepted. If slice 4 looks too big while slice 2 is in progress, deploy the receiver alone at the end of slice 2.

## 13. Open questions

1. **Roadmap wording.** The build-plan roadmap says "Terraform". With [ADR 0006](adr/0006-aws-cdk-python-for-iac.md), that becomes "IaC (CDK, Python)". The roadmap needs updating outside this repo.
2. **Case-study angle.** How much of the Instagram comparison goes in the README versus only in ADR 0002? Not blocking; decide in slice 7.
