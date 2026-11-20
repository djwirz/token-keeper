# Deploying token-keeper

One AWS account, one environment (`demo`), region `us-east-1`. The decisions behind this page are in [ADR 0014](adr/0014-stacks-share-urls-through-ssm.md), [ADR 0016](adr/0016-ci-deploys-through-oidc-and-bootstrap-roles.md) and [ADR 0017](adr/0017-dynamodb-condition-keys.md).

| Stack | Deployed by | Holds |
|---|---|---|
| `Budget` | hand, once, first | $5/month budget, email alerts at 50% and 100% |
| `CiRoles` | hand, once | GitHub OIDC provider, `DeployRole`, `DiffRole` |
| `DemoReceiver` | CI (after slice 4) | receiver API, Lambda, disposable table |
| `TokenKeeper` | CI (after slice 4) | Token Keeper API, Lambdas, queues, schedule, RETAIN table and KMS key |

Every command below assumes `AWS_PROFILE=token-keeper`, Node 22+, and a fresh Lambda build (`scripts/build_lambda.sh`).

**Operator access.** The account is on AWS's free account plan. That plan can't create an AWS Organization, so IAM Identity Center is out until the account is upgraded. Instead:
- Root has MFA and is used for nothing else.
- One IAM user with `AdministratorAccess`, MFA and an account alias, so the sign-in URL carries no account ID.
- The CLI signs in through the browser with `aws login --profile token-keeper` (AWS CLI 2.32+). Its credentials are short-lived, and no access keys exist anywhere.
- When the account moves to the paid plan, Identity Center can replace the IAM user.

## First deploy, in order

1. **The budget, before anything else.** It uses the legacy synthesizer (no assets, no bootstrap roles), so it needs no `cdk bootstrap`. The email is read from the main checkout's `.env.local`, never typed into a command:
   ```bash
   BUDGET_EMAIL="$(sed -n 's/^BUDGET_EMAIL=//p' .env.local)" npx cdk deploy Budget
   ```
   Budgets needs *IAM user and role access to Billing information* turned on, in the root user's Account page.
2. **Bootstrap CDK:** `npx cdk bootstrap` (the account and region come from the profile). This creates the asset bucket and the deploy, publishing, lookup and CloudFormation execution roles.
3. **The CI roles:** `npx cdk deploy CiRoles`.
4. **Secrets and the client ID:** `uv run python scripts/put_demo_secrets.py --profile token-keeper`.
   - It reads the GitHub App's client ID and secret from `.env.local`, and generates the webhook secret, connect-link secret and passcode.
   - It prints no values. `--show-passcode` reads the passcode back when you need it.
   - After an `--overwrite`, a running Lambda keeps the old value until its next cold start.
5. **The app stacks:** `npx cdk deploy DemoReceiver`, then `npx cdk deploy TokenKeeper`.
   - With URLs shared through SSM (ADR 0014), there's no third deploy.
   - Until both are up, a Lambda that needs the other stack's URL fails at cold start with "parameter not found".
6. **The GitHub App:** add the `TokenKeeper.CallbackUrl` stack output as a callback URL, next to the localhost one.

## CI setup (once, after `CiRoles`)

- **The `demo` environment:** GitHub → Settings → Environments → `demo`. Add Dan as required reviewer, and set deployment branches to `main` only.
- **Secrets,** read from the `CiRoles` outputs without printing them:
  ```bash
  out() { aws cloudformation describe-stacks --stack-name CiRoles --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
  out DiffRoleArn | gh secret set AWS_DIFF_ROLE_ARN
  out DeployRoleArn | gh secret set AWS_DEPLOY_ROLE_ARN --env demo
  ```
- **What then runs:** every PR from this repo gets a `cdk diff` comment. Every push to `main` runs `deploy.yml`, which waits for approval in `demo`.

## Forcing a refresh (proof, not routine)

A real refresh normally waits for the 8-hour access token to come within 10 minutes of expiry. To see one now, make the account due with an expiring token. That's an operator write, and it bumps `version` like every write (ADR 0015):

```bash
TABLE=$(aws cloudformation describe-stack-resources --stack-name TokenKeeper --query "StackResources[?ResourceType=='AWS::DynamoDB::Table'].PhysicalResourceId" --output text)
ACCOUNT=$(aws dynamodb get-item --table-name "$TABLE" --key '{"PK":{"S":"LINK#demo#u_demo"},"SK":{"S":"META"}}' --query Item.account_id.S --output text)
NOW=$(date +%s)
aws dynamodb update-item --table-name "$TABLE" \
  --key "{\"PK\":{\"S\":\"ACCOUNT#$ACCOUNT\"},\"SK\":{\"S\":\"META\"}}" \
  --update-expression "SET access_expires_at = :now, next_due_at = :now, version = version + :one" \
  --condition-expression "#s = :active" \
  --expression-attribute-names '{"#s":"state"}' \
  --expression-attribute-values "{\":now\":{\"N\":\"$NOW\"},\":one\":{\"N\":\"1\"},\":active\":{\"S\":\"active\"}}"
```

Within 15 minutes, the scheduled planner enqueues the account, and the worker refreshes under its lease, stores the rotated pair, syncs and delivers. The worker logs `refresh_succeeded`, and the receiver's "last synced" time moves.

## Proving the IAM conditions

```bash
uv run python scripts/prove_iam.py --profile token-keeper          # policy simulator, read-only
uv run python scripts/prove_iam.py --profile token-keeper --live   # real calls; creates and deletes temporary roles
```

Both modes print one line per case and `PASS` or `FAIL`. The account ID is masked. `--live` is the one that proves DynamoDB really enforces a condition (ADR 0017).
