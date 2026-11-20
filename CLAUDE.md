# token-keeper: instructions for Claude

A connector service that keeps third-party account connections healthy: it holds tokens, refreshes or re-verifies them on a schedule, and reports account state to a main app through one signed endpoint. First provider: GitHub. Use they/them for Dan.

## Status
Building: slice 6 of 7 (observability), per `docs/spec.md` §12. Operator steps are in `docs/deploy.md`; alarm responses in `docs/runbook.md`.

## How we work
- Pull requests only; never push to `main`. Merged PRs are the project's record.
- Spec first, then one thin end-to-end slice per PR. Start each slice in plan mode.
- Every decision that would be expensive to reverse gets an ADR in `docs/adr/` (format: ADR 0001). Don't decide silently in code.
- ADRs are written as `Accepted` in the PR that adds them; merging is the acceptance. Never rewrite a merged ADR's decision: add a new ADR that supersedes it.
- When the spec and the code disagree, stop and ask; update the spec in the same PR if the decision changes.
- Built with Claude Code in the open: the case study gets a section on how AI was used and where it was wrong, so note real mistakes in PR descriptions.

## Constraints
- Never employer code, data or names beyond what's public. Synthetic or own-account data only.
- Python where it fits (it's the default here).
- Free tiers first; AWS budget alarm before anything is deployed. A few dollars a month of LLM API use is fine.
- Secrets never in the repo, logs or PR text. Tokens are encrypted at rest.
- Target depth: deployed, tests and CI, ADRs, observability.
- Numbers in docs: honest estimates in words, never false precision.
