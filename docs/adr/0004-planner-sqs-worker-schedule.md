# 4. Scheduling: planner → SQS → worker with per-account lease

Date: 2026-11-18
Status: Accepted

## Context
Every connected account needs a sync about every 6 hours, a refresh before its 8-hour access token lapses, and backoff when things fail. GitHub refresh tokens are single-use, so two concurrent refreshes of one account, or one refresh with a stale refresh token, lose it.

## Options considered
1. **EventBridge Scheduler → planner λ → SQS (+DLQ) → worker λ.** The planner queries a `due` GSI every 15 minutes and enqueues per-account messages. Workers take a DynamoDB lease (a conditional write) before refreshing.
2. **One EventBridge Scheduler one-off schedule per account**, rescheduled after each run. Precise timing, but it's many schedules to manage and harder to see at a glance.
3. **A single cron λ that loops over accounts.** Simplest, but no per-account retry, no DLQ, and one slow account delays the rest.
4. **Step Functions per account.** Visible, but costlier and heavier than the problem needs.

## Decision
Option 1, with these rules:
- **Cadence:** 6 hours with jitter. Refresh happens when under 10 minutes of access-token life remain.
- **Planner:** queries `active` accounts with `next_due_at <= now`, pushes each one's `next_due_at` about 30 minutes ahead, then enqueues it, so a backed-up queue doesn't collect duplicates.
- **Lease:** a 60-second conditional update that also requires `state = active` and returns the item. The worker uses only the tokens from that response, and re-checks expiry against them, never values read before the lease. The Lambda timeout is set below the lease length.
- **Final write:** conditional on `lease_owner = me`. Writers outside the worker (reconnect, revocation) take over by clearing the lease and bumping `version`; a worker that loses its lease this way discards what it holds.
- **Transient errors:** the account stays `active`; `next_due_at` backs off, capped at about an hour, or follows the rate-limit reset. No provider-health logic (out of scope, spec §2).
- **Deliveries** go through a separate queue (ADR 0005), so a slow receiver can't stall syncing.

## Consequences
- Fan-out, retries and DLQs come from SQS rather than hand-rolled code.
- Scheduling granularity is 15 minutes, which is fine for 6-hour cadences.
- The `due` GSI partitions by state, which is a known hot-partition limit far beyond demo scale. Noted for the case study, not fixed.
- Double refresh needs a worker to outlive its lease, which the timeout prevents. Stale-token refresh is prevented by reading tokens from the lease write. Both have tests (spec §10).
