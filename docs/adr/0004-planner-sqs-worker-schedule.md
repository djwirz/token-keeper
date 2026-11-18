# 4. Scheduling: planner → SQS → worker with per-account lease

Date: 2026-11-18
Status: Proposed

## Context
Every connected account needs a sync about every 6 hours, a refresh before its 8-hour access token lapses, and backoff when things fail. GitHub refresh tokens are single-use, so two concurrent refreshes of one account lose it.

## Options considered
1. **EventBridge Scheduler → planner λ → SQS (+DLQ) → worker λ.** The planner queries a `due` GSI every 15 minutes and enqueues per-account messages. Workers take a DynamoDB lease (a conditional write) before refreshing.
2. **One EventBridge Scheduler one-off schedule per account**, rescheduled after each run. Precise timing, but it's many schedules to manage and harder to see at a glance.
3. **A single cron λ that loops over accounts.** Simplest, but no per-account retry, no DLQ, and one slow account delays the rest.
4. **Step Functions per account.** Visible, but costlier and heavier than the problem needs.

## Decision
Option 1. Sync cadence is 6 hours with jitter. Refresh happens when under 10 minutes of access-token life remain. Leases last 60 seconds and are released on completion. Deliveries go through a separate queue (ADR 0005), so a slow receiver can't stall syncing.

## Consequences
- Fan-out, retries and DLQs come from SQS rather than hand-rolled code.
- Scheduling granularity is 15 minutes, which is fine for 6-hour cadences.
- The `due` GSI partitions by state, which is a known hot-partition limit far beyond demo scale (spec §13).
- The lease makes double-refresh impossible except across a lease expiry, which needs a worker to stall for over 60 seconds mid-refresh. The Lambda timeout is set below the lease length so that can't happen.
