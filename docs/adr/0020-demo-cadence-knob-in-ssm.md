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
- **Reading:** the worker reads it through `SYNC_INTERVAL_MINUTES_PARAM`, and caches the value for 60 seconds of wall-clock time, so a change lands within a minute without a cold start. Locally, `SYNC_INTERVAL_MINUTES` in the environment takes its place.
- **When it's read:** before the worker takes its lease, with a fast SSM client (2-second connect and read timeouts, two retries). A slow or hung read then costs about ten seconds at most (three tries of a few seconds each), before any lease time is spent, rather than timing the worker out between the GitHub calls and the final write.
- **Bounds:** whole minutes from 15 to 360, in plain digits. Anything else, or a parameter the worker can't read (an error, a timeout or a network failure), means the normal six hours and a warning (`sync.interval_invalid` or `sync.interval_unreadable`). The fallback is cached like a good value, so the warning comes at most once a minute per warm worker. A bad knob never stops syncs.
- **Jitter:** ±30 minutes, or a quarter of the interval when that's smaller. At 15 minutes that's about ±4 minutes, not ±30.
- **Changing it is a hand change to AWS:** Dan approves it first, and it's put back to 360 once the recording is done (`docs/runbook.md`, "Sync cadence knob").

## Consequences
- **The effective demo cadence is coarser than the setting.** At 15 minutes, a sync falls due 15 minutes later, give or take about four, and the planner only looks every 15 minutes. So a sync comes about 10 to 35 minutes after the last one.
- **The knob applies from each account's next sync.** An account synced under six hours stays due up to six hours away. A reconnect applies it at once: the callback's immediate sync sets the next due time from the knob. A warm worker can hold the old value for up to a minute, so wait a minute after changing it before reconnecting.
- **Drift.** While the value is changed, the parameter differs from its template. We expect CloudFormation drift detection to report it; the live step in slice 7 records what it actually shows. A CI deploy that leaves the parameter's template unchanged doesn't put the value back, so putting it back stays a hand step. A deploy that does change it (a new description, say) writes `360` back, which is the safe direction.
- **Cost while it's short.** More metrics are emitted every hour, past spec §8's free-tier reasoning. Kept to the recording session, the cost should be a few cents at most.
- **Refresh is unchanged.** Refresh still happens only when the access token is near expiry, so a short cadence doesn't rotate tokens faster.
- **One more SSM read per warm worker per minute.** Standard parameters at that rate cost nothing.
