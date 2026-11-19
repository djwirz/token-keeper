# Token Keeper

Keeps third-party account connections alive: stores OAuth tokens, refreshes or re-verifies them on a schedule, and reports each account's state to a main app through one signed endpoint. First provider: GitHub.

**Status:** building, slice 2 of 7. Design: [docs/spec.md](docs/spec.md) and [docs/adr/](docs/adr/).

## Run it locally

Needs [uv](https://docs.astral.sh/uv/) (it fetches Python 3.12 itself). No AWS account or Docker: AWS calls (DynamoDB, SQS, KMS) go to an in-process [moto](https://github.com/getmoto/moto) server. GitHub is the real one.

**One-time: a GitHub App** (spec §5). At GitHub → Settings → Developer settings → GitHub Apps → New:
- Callback URL `http://localhost:8001/callback`; **Expire user authorization tokens** on; no permissions; webhook **Active** off for now.
- Generate a client secret, then add both values to `.env.local` (gitignored, created on first run):

```
GITHUB_CLIENT_ID=...
GITHUB_CLIENT_SECRET=...
```

Then:

```bash
uv sync
uv run python -m local.run
```

Open http://localhost:8000, enter the demo passcode (in `.env.local`) and approve the app on GitHub. You come back to the status page with `?result=connected`, and your profile appears once the deliverer has posted `account.connected` (a second or so). Token Keeper runs on :8001; without the GitHub App values only the receiver starts. Data lives in memory and is gone when the runner stops.

To send signed synthetic events straight to the receiver instead:

```bash
uv run python -m local.send_event --version 1
uv run python -m local.send_event --version 2 --state needs_reauth --reason revoked
```

## Checks

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

CDK synth (nothing is deployed before slice 4; needs Node 22+):

```bash
npm ci
scripts/build_lambda.sh
BUDGET_EMAIL=you@example.com npx cdk synth
```

## Layout

| Path | What |
|---|---|
| `src/token_keeper/contract/` | The contract a main app uses: webhook signing, connect links, event schema |
| `src/token_keeper/handlers/` | Token Keeper's Lambdas: `connect`, `callback`, `deliverer` |
| `src/token_keeper/` | Store (DynamoDB), KMS, GitHub client, token-scrubbing logger |
| `receiver/` | The demo receiver (the stand-in main app) |
| `local/` | Local runner and synthetic event sender |
| `infra/` | CDK app: `Budget`, `DemoReceiver` and `TokenKeeper` stacks |
| `tests/vectors/` | Test vectors from independent sources |
