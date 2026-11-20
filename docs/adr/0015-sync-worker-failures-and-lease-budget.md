# 15. Sync worker: where failures live, and the lease's time budget

Date: 2026-11-19
Status: Accepted

## Context
ADR 0004 set the shape: planner → SQS → worker, with a 60-second lease. Slice 3 is the first code to run it, and a few choices it left open are expensive to change once accounts are deployed, because they decide whether a refresh can ever run twice:

- **Where GitHub trouble is recorded.** Either SQS retries the message, or the account itself records the failure and its next due time.
- **How long a worker may run** compared with its lease. A worker still refreshing after its lease has passed to another worker can use a refresh token the other worker has already rotated. GitHub then rejects it, and the account looks revoked (spec §5 "The rotation hazard").
- **Whether bookkeeping writes bump `version`.** The planner's push and the lease itself are writes, and spec §4 says every write bumps it. `version` is part of the webhook contract.

## Options considered
1. **Failures in the account; the message only fails on bugs.** A transient GitHub error writes `last_error_code`, `consecutive_failures` and a backed-off `next_due_at`, and the message is consumed. Only an unexpected exception fails the message, which then goes to a DLQ after three receives.
2. **Failures as message retries.** GitHub errors fail the message, and SQS visibility timeouts do the backoff. This needs no account fields. But the lease, the backoff and the DLQ would all fire for an ordinary GitHub outage, and a 5xx storm would fill the DLQ and page someone for something spec §2 says not to handle.
3. **A deadline inside the worker instead of a Lambda timeout below the lease.** This is more flexible, but it relies on every code path checking the deadline. A timeout is enforced by Lambda whatever the code does.

## Decision
Option 1, plus these rules:
- **Time budget:**
  - The worker Lambda's timeout is 40 seconds, against a 60-second lease. A CDK assertion test ties the two together, and the stack refuses to synth otherwise.
  - The worker also won't start a token-endpoint call with less than 20 seconds of lease left. That protects the local runner, which has no timeout, and any future change to the numbers.
  - The worker takes one message per invocation, and the sync queue's visibility timeout is six times the worker's timeout.
- **Backoff:**
  - Transient errors back off 15, then 30, then 60 minutes, capped there. The planner runs every 15 minutes, so finer steps would buy nothing.
  - A rate limit uses GitHub's reset or `retry-after` time, kept between a minute and an hour.
  - No event is sent for either (ADR 0011).
- **DLQ:** the sync DLQ takes a message after three receives. Only bugs, or AWS itself failing, get there.
- **Versions:**
  - Every Account write bumps `version`: the planner's push, the lease, a stored refresh, and the final write. Versions seen by the main app can therefore skip numbers.
  - Receivers already keep the highest version (ADR 0012), so gaps are harmless.
  - Keeping one rule means a writer's `version = :old` condition (the callback's reconnect) also notices a concurrent planner or worker write.
- **The `due` index projects keys only**, so no token ciphertext is copied into it.

## Consequences
- A long GitHub outage leaves accounts `active`, retrying hourly, with no DLQ noise and no alarms. That's the honest picture from ADR 0011.
- `last_error_code` and `consecutive_failures` on the account show why an account is stale, which answers spec §1's "why did this account stop syncing?".
- A worker never outlives its lease, so a double refresh needs Lambda to break its own timeout. The race and stale-read tests (spec §10) cover the rest.
- A `needs_reauth` event can still be lost if the delivery enqueue fails right after the final write: a retried message finds the account no longer `active` and stops. That's rare (an SQS send failure) and is noted as a known limitation, not handled.
- If the numbers change later, the CDK test and the in-code guard both need to agree, which is the point.
