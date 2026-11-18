# 5. Webhook contract: Standard Webhooks, full-state events

Date: 2026-11-18
Status: Accepted

## Context
Token Keeper reports to the main app through exactly one endpoint. That endpoint must be authenticated, replay-resistant, safe to retry, and safe to receive out of order.

## Options considered
1. **The Standard Webhooks spec** (HMAC-SHA256 over `id.timestamp.body`, the `webhook-id`/`webhook-timestamp`/`webhook-signature` headers, multi-signature rotation), carrying **full current state** plus a per-account `version`.
2. **A custom HMAC header carrying delta events.** Bespoke, and deltas need ordering guarantees we don't have.
3. **Asymmetric (Ed25519) signatures.** The receiver holds only a public key. Valuable with many tenants, unnecessary with one.
4. **Pull API** (the main app polls). Moves the retry burden to the consumer and adds a second interface.

## Decision
Option 1. Events are `account.connected` and `account.updated`, each with the full account state and snapshot; states and reasons are fixed in ADR 0011.

- `webhook-id` is created when the event is enqueued and kept across retries.
- The body is built from the account's current state when the event is sent, and each attempt gets a fresh timestamp and signature.
- The receiver dedupes on `webhook-id`, rejects timestamps more than 5 minutes off, and keeps the highest `version` per account.
- Delivery uses its own SQS queue, retries with exponential backoff for about a day, and has a DLQ and an alarm.

## Consequences
- Reviewers recognise the format, and published test vectors give the signing code an external check.
- Retries and reordering are harmless. For an `active` account, the next sync's event heals a missed one. A `needs_reauth` account isn't synced again, so a lost event for it needs a DLQ redrive (runbook).
- Payloads are a little larger than deltas, which is irrelevant at this size.
- Moving to asymmetric signatures later is a contained change (Standard Webhooks defines `v1a`).
