# 14. The stacks share URLs through SSM, not CloudFormation exports

Date: 2026-11-19
Status: Accepted

## Context
Each of the two app stacks needs the other's URL. `DemoReceiver` needs Token Keeper's `/connect` URL to sign connect links and to allow it in its CSP. `TokenKeeper` needs the receiver's URL, because it's the one allowed `return_to` and the base of the webhook endpoint. Token Keeper also needs the GitHub App's client ID, which only exists once the App is registered.

The first version in PR 3 passed the receiver's URL to `TokenKeeper` as a cross-stack reference (a CloudFormation export and import). The receiver got Token Keeper's URL from an environment variable at synth time, with a placeholder default; so did the client ID. The fresh-eyes review of PR 3 pointed out the costs:
- A strong export can't change while it's imported, so replacing the receiver's API would need a two-step deploy.
- The first deploy needs a fixed order plus a redeploy: receiver, Token Keeper, then the receiver again with the real URL.
- A deploy without the environment variables quietly falls back to the placeholders. A `client_id` of `unset` gives the same bare 404 after GitHub sign-in that cost time in this slice.

Nothing is deployed yet, so changing this now is cheap. After the first deploy the export is in use and harder to remove.

## Options considered
1. **Keep the export and the environment variables**, but fail the synth when a placeholder would reach a deploy, and document the deploy order.
2. **Non-secret SSM String parameters.** Each stack writes its own URL. Each Lambda reads the other's at cold start, by name, the same way it already reads its secrets. The client ID is a hand-made parameter, like the secrets.
3. **Custom domains** (e.g. `connect.<domain>`), known before any deploy. That costs a domain and certificates, which is more than the demo needs.

## Decision
Option 2. The parameters are under `/token-keeper/demo/shared/` (`connect-url`, `receiver-url`, `github-client-id`; names in `infra/stacks/shared.py`). Each Lambda gets `…_PARAM` environment variables and `ssm:GetParameter` on exactly the parameters it reads. Locally, the runner sets the plain variables instead, through the same `env_or_ssm` helper.

## Consequences
- No exports or imports. The stacks deploy in any order, and either can be replaced on its own.
- No synth-time environment variables, so CI can't deploy a placeholder by accident.
- Until both stacks are deployed, a Lambda that needs the other stack's URL fails at cold start with a clear "parameter not found" error. After that, a Lambda keeps the value until its next cold start, so a changed URL takes effect when the container is recycled. Neither matters for a demo whose URLs don't change.
- Two more cold-start SSM reads per Lambda, inside the free tier.
