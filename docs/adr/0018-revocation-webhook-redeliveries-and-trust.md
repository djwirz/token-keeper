# 18. Revocation webhook: what it trusts, and what a redelivery does

Date: 2026-11-20
Status: Accepted

## Context
ADR 0010 chose GitHub's `github_app_authorization` webhook for instant revocation, and spec §4 gave it the takeover write slice 3 built (`mark_needs_reauth_takeover`). Slice 5 is the first code to receive a real delivery. Some of its choices are expensive to change once GitHub is pointed at the endpoint and accounts depend on it:

- **What a redelivery does.** GitHub doesn't redeliver on its own: a failed delivery stays failed until someone presses Redeliver in the App's Recent Deliveries, within three days. GitHub's docs say a redelivery keeps the original `X-GitHub-Delivery` GUID. As slice 3 built it, the takeover wrote every time, so a repeat bumped `version` and sent another event (PR 4, "Known limitations"). Worse, a redelivery pressed *after* the user reconnected would revoke the new, working grant.
- **What the signature covers.** `X-Hub-Signature-256` is an HMAC of the body only. The event name (`X-GitHub-Event`) and the delivery GUID are unsigned headers.
- **What the handler's role can do,** and which of its IAM conditions are kept (ADR 0017's rule: only what a deployed test proves).
- **Where the secret lives,** and how it reaches GitHub's settings without being printed.

## Options considered
For redeliveries:
1. **Write every time** (slice 3's behaviour). Simple. Harmless to the receiver, which ignores equal or lower versions (ADR 0012), but each repeat bumps `version`, and a stale redelivery after a reconnect forces a needless re-auth.
2. **No write if already revoked; record the delivery GUID and skip it if seen.** One extra attribute and a longer condition. A redelivery before or after a reconnect changes nothing.
3. **A separate dedupe table (or items) of delivery GUIDs.** Complete, but a new key prefix, a TTL and a second write per delivery, for the one case option 2 already covers.

For the event name: trust the header alone, or require the signed body to agree with it.

## Decision
Option 2, plus these rules:

- **The takeover is conditional.** It writes only when the account exists and is not already `needs_reauth` for `revoked`, *and* this delivery's GUID isn't the one it last recorded (`revoked_delivery`). It records the GUID when it writes. A revocation that lands on another `needs_reauth` reason (say `refresh_rejected`) still writes `revoked`: GitHub knows better than our guess.
- **An event is enqueued even when nothing was written.** The deliverer sends the current state, so the receiver sees an equal version and keeps what it had. If the first attempt's SQS send failed (a 5xx back to GitHub), a redelivery is how that event is sent at all.
- **The signed body has to agree with the header.** The handler acts only if `X-GitHub-Event` is `github_app_authorization` *and* the body's `action` is `revoked` *and* `sender.id` is an integer. A signed body from another event, such as a `ping` (which also carries a `sender`), can't be replayed under a revocation's event name. GitHub's docs don't list the `action` value for this event; Octokit's schema says `revoked`. The first real delivery settles it.
- **Order and answers.** Verify the signature first (constant-time compare over the raw body, as GitHub's docs ask), then parse. A missing or bad signature gets 401 and nothing is parsed. A signed body that's malformed gets 400. Unknown users, other events and no-op repeats get 204 with a log line. An unexpected error gets 500, which shows as a failure in Recent Deliveries. The Lambda timeout is 8 seconds, under GitHub's 10.
- **IAM:**

  | Role | Actions | Condition |
  |---|---|---|
  | github-webhook | `GetItem` | `LeadingKeys` like `GHUSER#*` |
  | github-webhook | `UpdateItem` | `LeadingKeys` like `ACCOUNT#*` |
  | github-webhook | `SendMessage` (delivery queue), its SSM parameter | none |

  No KMS: it can't read or write a token. There's no `dynamodb:Attributes` condition: the takeover has to name the token fields to remove them, and IAM can't tell a REMOVE from a SET, so the condition wouldn't guard what matters. `scripts/prove_iam.py` gains this role's cases. Its allowed requests go through `Store` itself. Both conditions stay only if the live probe passes them after the first deploy. Otherwise a new ADR drops them.
- **The secret** is its own SecureString, `/token-keeper/demo/token-keeper/github-webhook-secret`. `put_demo_secrets.py --github-webhook-secret` generates it (32 random bytes, as hex), writes it to SSM, and puts it on the clipboard for pasting into the App's settings. It's never printed and never written to disk.
- **Proving it live without a laptop deploy.** The PR merges on local tests, CI deploys it with Dan's approval (ADR 0016), and the live revocation runs afterwards. Fixes, and the record of what the live run showed, go in a small follow-up PR.

## Consequences
- A redelivery is a no-op, before or after a reconnect, and `version` moves only when the state does. A fresh revocation after a reconnect still flips the card: it has a new GUID.
- **The GUID guard depends on GitHub keeping the GUID on redelivery.** GitHub's best-practices page says it does, and the live run checks it by redelivering after a reconnect.
- **A stated limitation: replay.** GitHub's signature carries no timestamp, and the GUID is an unsigned header. Someone who captured a real delivery could replay the body with a new GUID and force a re-auth. That's the worst case spec §9 already accepts. They can't read or write a token.
- **A stated limitation: a dropped delivery** is caught only by the next sync's 401, as `unauthorized` rather than `revoked` (ADR 0010).
- The recorded deliveries in `tests/vectors/github_deliveries.json` follow GitHub's documented shape, with synthetic ids. They're checked against a real delivery from Recent Deliveries after the live run, since twice in slice 4 a test pinned our own idea of the other side rather than what it really sent.
- Metrics for received and rejected deliveries (`RevocationReceived`, spec §8) come with slice 6.
