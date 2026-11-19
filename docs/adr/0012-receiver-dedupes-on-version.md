# 12. The receiver deduplicates on version, not webhook-id

Date: 2026-11-18
Status: Accepted. Supersedes the "dedupes on `webhook-id`" part of ADR 0005.

## Context
ADR 0005 has the receiver dedupe on `webhook-id` and also keep the highest `version` per account. It also says Token Keeper keeps `webhook-id` across retries but rebuilds the body from current state on each attempt.

Those two rules overlap, and in one case they conflict. Suppose attempt 1 is stored but its 2xx is lost, and the account changes before attempt 2. Attempt 2 carries a higher version under the same id, and id-based dedupe drops it. Nothing is lost today, because that state change sends its own event with its own id. But the id check adds nothing the version check doesn't already do.

The fresh-eyes review of PR 2 found this. The first receiver used a transaction with a TTL'd `MSG#<id>` marker, and a test locked in the drop.

## Options considered
1. **Version only.** One conditional write: store the event if there is no stored version or the stored one is lower. A redelivery, an older event or an equal version is a no-op.
2. **Keep both checks and document the interaction.** No code change, but it keeps a second item type, a TTL, a transaction and a rule that can only make things worse.
3. **Dedupe on id, but let a higher version through.** That is option 1 with extra steps.

## Decision
Option 1. The receiver stores an event only when its `version` is higher than the stored one. `webhook-id` is still sent (Standard Webhooks requires it, and the signature covers it) and is still logged, so a delivery can be traced across Token Keeper and receiver logs.

## Consequences
- The receiver is one conditional `PutItem`, with no marker items and no TTL. Its IAM grant shrinks to `PutItem` and `Scan`.
- A main app needs only the rule "keep the highest version per account". That is the rule any full-state contract needs anyway, so it is the simplest one to document for a second main app.
- Two events with the same version but different types (say `account.connected` and an `account.updated` read from the same state) keep whichever arrives first. Both carry the same state, so that's fine.
