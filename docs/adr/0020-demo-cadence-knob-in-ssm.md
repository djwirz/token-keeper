# 20. A demo cadence knob in SSM, changed by hand only to record

Date: 2026-11-22
Status: Accepted

## Context
Spec §4 promises a demo knob that shortens the six-hour sync cadence, so the README GIF can show a *scheduled* sync moving the card's "last synced" time (acceptance criterion 1). Until slice 7 the interval was a constant in `sync.py`, so on the deployed `demo` a scheduled sync came about six hours after the last one.

Constraints that shape the knob:
- **The planner runs every 15 minutes** (ADR 0004). An interval shorter than that buys nothing.
- **Cost.** Spec §8 keeps the custom metrics inside CloudWatch's free tier because only `AccountsDue` is emitted around the clock. A short cadence left on would emit the sync, refresh and delivery metrics every hour too.
- **Deploys go through CI from `main`** (ADR 0016), with Dan's approval. A knob that needs a deploy each way costs two PRs and two approvals per recording.
- **The GIF should come from `demo`**, not localhost, because that's what criterion 1 describes.

## Options considered
1. **An SSM parameter that `TokenKeeper` creates at 360 minutes.** The worker re-reads it at most once a minute. It's changed by hand with `put-parameter` for a recording, then put back.
2. **A worker environment variable from CDK context.** No drift, and the value is always in the template. But every change is a PR and an approved CI deploy, twice per recording.
3. **Local only:** a setting for the local runner, with `demo` fixed at six hours. Simplest, but the GIF would show localhost.

## Decision
Option 1:
- **Parameter:** `/token-keeper/demo/token-keeper/sync-interval-minutes`, a plain String. `TokenKeeper` creates it with the value `360`. Only the worker's role can read it.
- **Reading:** the worker reads it through `SYNC_INTERVAL_MINUTES_PARAM`, and caches the value for 60 seconds, so a change lands within a minute without a cold start. Locally, `SYNC_INTERVAL_MINUTES` in the environment takes its place.
- **Bounds:** whole minutes from 15 to 360. Anything else, or a parameter the worker can't read, means the normal six hours and one warning log line (`sync.interval_invalid` or `sync.interval_unreadable`). A bad knob never stops syncs.
- **Jitter:** ±30 minutes, or a quarter of the interval when that's smaller. At 15 minutes that's about ±4 minutes, not ±30.
- **Changing it is a hand change to AWS:** Dan approves it first, and it's put back to 360 once the recording is done (`docs/runbook.md`, "Sync cadence knob").

## Consequences
- **The effective demo cadence is coarser than the setting.** The planner only looks every 15 minutes, so a 15-minute setting means a sync roughly every 15 to 30 minutes.
- **The knob applies from each account's next sync.** An account synced under six hours stays due up to six hours away. A reconnect applies it at once: the callback's immediate sync sets the next due time from the knob.
- **Drift.** While the value is changed, the parameter differs from its template. We expect CloudFormation drift detection to report it; the live step in slice 7 records what it actually shows. A CI deploy that leaves the parameter's template unchanged doesn't put the value back, so putting it back stays a hand step.
- **Cost while it's short.** More metrics are emitted every hour, past spec §8's free-tier reasoning. Kept to the recording session, the cost should be a few cents at most.
- **Refresh is unchanged.** Refresh still happens only when the access token is near expiry, so a short cadence doesn't rotate tokens faster.
- **One more SSM read per warm worker per minute.** Standard parameters at that rate cost nothing.
