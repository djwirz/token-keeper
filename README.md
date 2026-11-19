# Token Keeper

Keeps third-party account connections alive: stores OAuth tokens, refreshes or re-verifies them on a schedule, and reports each account's state to a main app through one signed endpoint. First provider: GitHub.

**Status:** building, slice 1 of 7. Design: [docs/spec.md](docs/spec.md) and [docs/adr/](docs/adr/).

## Run it locally

Needs [uv](https://docs.astral.sh/uv/) (it fetches Python 3.12 itself). No AWS account or Docker: AWS calls go to an in-process [moto](https://github.com/getmoto/moto) server.

```bash
uv sync
uv run python -m local.run
```

Open http://localhost:8000. The runner writes dev secrets, including the demo passcode, to `.env.local` (gitignored) on first run. Data lives in memory and is gone when it stops.

In another terminal, send signed synthetic events (Token Keeper's deliverer arrives in slice 2):

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
| `receiver/` | The demo receiver (the stand-in main app) |
| `local/` | Local runner and synthetic event sender |
| `infra/` | CDK app: `Budget` and `DemoReceiver` stacks |
| `tests/vectors/` | Test vectors from independent sources |
