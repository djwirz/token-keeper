# Token Keeper

Keeps third-party account connections alive: stores OAuth tokens, refreshes or re-verifies them on a schedule, and reports each account's state to a main app through one signed endpoint. First provider: GitHub.

**Status:** v1 is built, deployed and proven live, apart from one step: the README GIF, which is still to be recorded by hand ([docs/deploy.md](docs/deploy.md), "Recording the README GIF"). Design: [docs/spec.md](docs/spec.md) and [docs/adr/](docs/adr/). How it was built: [docs/case-study.md](docs/case-study.md).

<!-- The demo GIF (connect, a scheduled sync, revoke, reconnect) goes here in slice 7's live-proof PR. -->

## Why GitHub

Instagram Login's long-lived token and a GitHub App's user tokens pose the same problem: a credential that lapses at a hard cliff unless it's renewed in time, and re-auth as the only way back. Instagram uses one 60-day token that you refresh yourself; GitHub splits it into an 8-hour access token and a 6-month refresh token. A connector for either has to track expiry, renew well before the cliff, tell a passing error from a user who's gone, and tell the product. GitHub adds one twist: each refresh token works only once, so two concurrent refreshes would lose the account, and the worker refreshes only under a lease. The comparison and the alternatives are in [ADR 0002](docs/adr/0002-github-app-with-expiring-user-tokens.md).

## Run it locally

Needs [uv](https://docs.astral.sh/uv/) (it fetches Python 3.12 itself). No AWS account or Docker: AWS calls (DynamoDB, SQS, KMS) go to an in-process [moto](https://github.com/getmoto/moto) server. GitHub is the real one.

**One-time: a GitHub App** (spec §5). At GitHub → Settings → Developer settings → GitHub Apps → New:
- Callback URL `http://localhost:8001/callback`; **Expire user authorization tokens** on; no permissions; webhook **Active** off for now.
- Generate a client secret, then add it and the **Client ID** (it starts with `Iv`; the numeric App ID won't work) to `.env.local` (gitignored, created on first run):

```
GITHUB_CLIENT_ID=...
GITHUB_CLIENT_SECRET=...
```

Then:

```bash
uv sync
uv run python -m local.run
```

Open http://localhost:8000, enter the demo passcode (in `.env.local`) and approve the app on GitHub. You come back to the status page with `?result=connected`, and your profile appears once the deliverer has posted `account.connected` (a second or so). The callback also enqueues an immediate sync, so your recent public activity follows a moment later. The runner runs the planner every 30 seconds in place of the 15-minute schedule. Accounts sync about every six hours; to watch scheduled syncs locally, add `SYNC_INTERVAL_MINUTES=15` to `.env.local` (15 to 360, [ADR 0020](docs/adr/0020-demo-cadence-knob-in-ssm.md)). To force a real refresh-token rotation without waiting 8 hours, `curl -X POST http://127.0.0.1:8002/expire`: it marks access tokens as expired and runs the planner, so the next sync refreshes against GitHub. Token Keeper runs on :8001; without the GitHub App values only the receiver starts. Data lives in memory and is gone when the runner stops.

To send signed synthetic events straight to the receiver instead:

```bash
uv run python -m local.send_event --version 1
uv run python -m local.send_event --version 2 --state needs_reauth --reason revoked
```

## Checks

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

CDK synth (needs Node 22+; `Budget` is only synthesized when `BUDGET_EMAIL` is set):

```bash
npm ci
scripts/build_lambda.sh
BUDGET_EMAIL=you@example.com npx cdk synth
```

## Deploy

One environment, `demo`. PRs get a `cdk diff` comment through a read-only OIDC role, and merges to `main` deploy `DemoReceiver` and `TokenKeeper` after approval in the GitHub `demo` environment. `Budget` and `CiRoles` are deployed by hand. First-deploy order, CI setup and operator steps: [docs/deploy.md](docs/deploy.md).

## Layout

| Path | What |
|---|---|
| `src/token_keeper/contract/` | The contract a main app uses: webhook signing, connect links, event schema |
| `src/token_keeper/handlers/` | Token Keeper's Lambdas: `connect`, `callback`, `planner`, `worker`, `deliverer` |
| `src/token_keeper/` | Store (DynamoDB), KMS, GitHub client, token-scrubbing logger |
| `receiver/` | The demo receiver (the stand-in main app) |
| `local/` | Local runner and synthetic event sender |
| `infra/` | CDK app: `Budget`, `CiRoles`, `DemoReceiver` and `TokenKeeper` stacks |
| `scripts/` | Lambda build, demo secrets, IAM proof |
| `tests/vectors/` | Test vectors from independent sources |
