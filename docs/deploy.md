# Deploying token-keeper

One AWS account, one environment (`demo`), region `us-east-1`. The decisions behind this page are in [ADR 0014](adr/0014-stacks-share-urls-through-ssm.md), [ADR 0016](adr/0016-ci-deploys-through-oidc-and-bootstrap-roles.md) and [ADR 0017](adr/0017-dynamodb-condition-keys.md).

| Stack | Deployed by | Holds |
|---|---|---|
| `Budget` | hand, once, first | $5/month budget, email alerts at 50% and 100% |
| `CiRoles` | hand, once | GitHub OIDC provider, `DeployRole`, `DiffRole` |
| `Alerts` | hand, once (slice 6) | SNS topic `token-keeper-alarms` and the alarm email subscription |
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
   - SSM has no transaction. If a write fails partway, the script says so, and a shared secret may differ between the stacks (every delivery would then fail its signature check). Rerun with `--overwrite`.
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
- **The OIDC subject:** check which form GitHub sends before deploying `CiRoles`. This repo uses immutable subjects, which carry the owner and repo IDs; `infra/stacks/ci_roles.py` must match the prefix this prints:
  ```bash
  gh api repos/djwirz/token-keeper/actions/oidc/customization/sub --jq .sub_claim_prefix
  ```
- **`main` is not branch-protected** (no ruleset, no required checks). `deploy.yml` runs on every push to `main` whatever CI's result, so the `demo` approval is the only gate. A ruleset requiring a PR and the "Lint, types, tests", "Lambda build and CDK synth" and "Secret scan" checks would close that; the diff job stays optional, since it's the first thing to cut.

## The revocation webhook (slice 5)

GitHub tells Token Keeper when Dan revokes the App, through `POST /github/webhook` ([ADR 0018](adr/0018-revocation-webhook-redeliveries-and-trust.md)). Once the stack with the `GitHubWebhook` Lambda has deployed through CI:

1. **The secret,** generated, written to SSM and put on the clipboard, never printed:
   ```bash
   uv run python scripts/put_demo_secrets.py --profile token-keeper --github-webhook-secret
   ```
   It refuses if the parameter exists. With `--overwrite` it makes a new one, and the App's setting must then be pasted again. A warm Lambda keeps the old value, and answers 401 to every real delivery until it's recycled. GitHub never retries, so after an overwrite:
   - force a cold start, where `$FN` is the `GitHubWebhook` function's name from the stack's resources. Change the description, then clear it again. Each update replaces the warm instances, and clearing it leaves the function matching the template. The template sets no description, so CloudFormation won't clear one left behind, and a deploy with no changes doesn't touch the function at all (seen 2026-11-20):
     ```bash
     aws lambda update-function-configuration --function-name "$FN" --description "secret rotated" && aws lambda wait function-updated-v2 --function-name "$FN"
     aws lambda update-function-configuration --function-name "$FN" --description "" && aws lambda wait function-updated-v2 --function-name "$FN"
     ```
   - then press Redeliver on anything that failed in Recent Deliveries.
2. **The GitHub App**, only once the deploy has finished (GitHub sends its `ping` when the webhook is saved, and doesn't retry a 404): Settings → Developer settings → GitHub Apps → the App → General → Webhook:
   - tick **Active**;
   - set the URL to the `TokenKeeper.GitHubWebhookUrl` stack output;
   - paste the secret;
   - click **Save changes** at the bottom of the page, then copy something else to clear the clipboard.

   The App receives `github_app_authorization` by default and can't unsubscribe from it.
3. **Check it, before anyone revokes anything.** Prove that GitHub signs with the secret Token Keeper has. In the App's Advanced tab, press Redeliver on the `ping`: Recent Deliveries must show **204**, and the Lambda log `github_webhook.ignored` with `ignored_event`. A 401 with `missing_signature` means the App has no secret saved, and `bad_signature` means it holds a different one. In the first live run, the secret hadn't saved and the first revocation got a 401 (2026-11-20). GitHub signs a redelivery with the App's *current* secret, so after fixing the secret, redelivering the failed delivery works. A forged request must also get a 401:
   ```bash
   URL=$(aws cloudformation describe-stacks --stack-name TokenKeeper --query "Stacks[0].Outputs[?OutputKey=='GitHubWebhookUrl'].OutputValue" --output text)
   curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'X-GitHub-Event: ping' -H 'X-Hub-Signature-256: sha256=00' -d '{}' "$URL"
   ```
4. **Rerun the IAM proof** (below). It has the webhook role's cases.

A redelivery from Recent Deliveries is harmless when the original attempt got a 2xx: it keeps the original delivery GUID, which the account has recorded, so it writes nothing, even after a reconnect. A redelivery of an attempt that failed (a 5xx, or a 401 after a secret change) may never have been recorded. After a reconnect, it revokes the new grant: GitHub's payload has no timestamp to tell a stale revocation from a fresh one. So don't redeliver a failed revocation once Dan has reconnected.

## Alarms (slice 6)

The alarms live in `TokenKeeper`, which CI deploys. They notify the `token-keeper-alarms` topic, which lives in `Alerts`, a hand-deployed stack like `Budget`, so the address never reaches GitHub ([ADR 0019](adr/0019-observability-as-built.md)). What to do when one fires: [runbook.md](runbook.md).

1. **The address:** add `ALERT_EMAIL=…` to the main checkout's `.env.local`. It can be the budget's address.
2. **Deploy `Alerts`,** ideally before the CI deploy that adds the alarms. Alarms that fire before the topic exists change state but email no one. Like `Budget`, it uses the legacy synthesizer, and the address is read from the file, never typed:
   ```bash
   ALERT_EMAIL="$(sed -n 's/^ALERT_EMAIL=//p' .env.local)" npx cdk deploy Alerts
   ```
3. **Confirm the subscription:** AWS Notifications emails a link; click **Confirm subscription**. The budget's confirmation email looks the same, so check the email names `token-keeper-alarms`, and that the page you land on says the subscription is confirmed. Never click "unsubscribe" in an alarm email: it silently ends every notification. Check without printing the address (`email True` means confirmed):
   ```bash
   TOPIC=$(aws sns list-topics --query "Topics[?ends_with(TopicArn, ':token-keeper-alarms')].TopicArn" --output text)
   aws sns list-subscriptions-by-topic --topic-arn "$TOPIC" --query 'Subscriptions[].[Protocol, SubscriptionArn != `PendingConfirmation`]' --output text
   ```
4. **Expect three "OK" emails** right after the first deploy that creates the alarms, one per alarm. An OK action fires on any change to OK, including an alarm's first evaluation. They aren't a drill or a recovery. A replaced alarm (a renamed one, say) sends one again.
5. **Prove the email path** once the alarms are deployed. This flips one alarm by hand, and its next evaluation (within five minutes) puts it back, so two emails arrive. It changes no template property:
   ```bash
   aws cloudwatch set-alarm-state --alarm-name token-keeper-refresh-failed --state-value ALARM --state-reason "email path check"
   ```

## Forcing each alarm (proof, not routine)

Spec §8 asks for each alarm to be fired once, live, with the email arriving. None of these changes a template property, so the stack stays in sync. Run them one at a time, so each email is easy to place. Shell helpers (`res`, `logs`) are at the top of [runbook.md](runbook.md). How long each took on 2026-11-21 is in [ADR 0019](adr/0019-observability-as-built.md), "Proven live".

**DLQ (about a quarter of an hour to ALARM).** Both DLQs must be empty first (runbook, "DLQ messages"). A body that isn't JSON makes the worker raise; after three receives SQS moves it to `SyncDlq`:
```bash
aws sqs send-message --queue-url "$(res AWS::SQS::Queue SyncQueue)" --message-body 'alarm drill: not json'
```
Expect `logs Worker '"sync.error"'` three times, then the ALARM email. Recover with the runbook's "Delete a poison message": look at the body first. To prove the redrive as well, put a valid delivery message for a real account straight on `DeliveryDlq`, wait for the ALARM, then use the runbook's redrive, which names the destination (a hand-placed message has no source queue to go back to):
```bash
aws sqs send-message --queue-url "$(res AWS::SQS::Queue DeliveryDlq)" \
  --message-body '{"account_id": "acc_…", "type": "account.updated", "webhook_id": "msg_…"}'
```
Expect `delivery.succeeded` in `logs Deliverer`, an empty DLQ, and the OK email. The receiver ignores the equal version.

**RefreshFailed (a few minutes to ALARM, a little over an hour to OK).** A synthetic account with a fake refresh token and an expired access token; GitHub answers `bad_refresh_token`:
```bash
uv run python scripts/account_ops.py --profile token-keeper create-synthetic --send-sync
```
Expect `refresh.failed` with `reason` `token_error:bad_refresh_token`, the account `needs_reauth` (`refresh_rejected`), and the ALARM email. The demo receiver shows a `u_alarm_drill` card until it's deleted with the runbook's own procedure (the account id is positional):
```bash
uv run python scripts/account_ops.py --profile token-keeper delete --receiver acc_…
```

**PlannerMissing (about an hour to ALARM).** Save the schedule, then send it back with only `State` changed. `update-schedule` replaces the whole definition:
```bash
SCHEDULE=$(res AWS::Scheduler::Schedule PlannerSchedule)
DIR=$(mktemp -d)
aws scheduler get-schedule --name "$SCHEDULE" > "$DIR/schedule.json"
jq 'del(.Arn, .CreationDate, .LastModificationDate) | .State = "DISABLED"' "$DIR/schedule.json" > "$DIR/disabled.json"
aws scheduler update-schedule --cli-input-json "file://$DIR/disabled.json"
```
Re-enable it as soon as the ALARM email arrives, with the same jq and `"ENABLED"`. Then check `get-schedule` matches the saved copy apart from `LastModificationDate`, and run drift detection (runbook, "Planner missing"). While it's off, no account syncs on schedule; the first run after picks them all up.

## Recording the README GIF (slice 7)

The GIF shows acceptance criterion 1 (spec §11) on the deployed `demo`: connect, a *scheduled* sync, revocation and reconnect. Dan records it with their own screen recorder, in their own browser, where they're already signed in to GitHub.

**Before recording:**
1. Set the cadence knob to 15 minutes (runbook, "Sync cadence knob"). This is a hand change to AWS, so ask first.
2. **Wait a minute** before connecting. A warm worker can hold the old six-hour value for up to a minute, and a connect inside that minute schedules the next sync six hours out.
3. Check the demo account has public GitHub activity from the last few weeks, so the card isn't empty (spec §5).

**Shot list,** one take or several, cut together:
1. **The status page,** with the card as it stands.
2. **Connect:** unlock with the passcode, click "Connect GitHub", approve on GitHub. Back on the page, the card says "Connected", with the profile and recent activity. The reconnect also applies the 15-minute knob from here on. Before waiting, check the account's next due time is about 15 minutes out (runbook, "Sync cadence knob").
3. **A scheduled sync:** wait for it, then reload. Show "Last synced" moving on. Cut the wait: it can be anything from about 10 to 35 minutes.
4. **Revoke:** GitHub → Settings → Applications → Authorized GitHub Apps → revoke the app. Back on the page, reload: the card says "Needs re-auth (revoked)" within seconds.
5. **Reconnect:** "Connect GitHub" again, approve, and the card says "Connected" again.

**Recording rules:**
- The browser window only, with no bookmarks bar, other tabs or extensions showing.
- No email address, AWS console, account ID or passcode in frame. Type the passcode off-camera, or cut that part.
- On GitHub's Authorized GitHub Apps page, which lists every app you've authorized, frame only the token-keeper row, or cut to just after the revoke.
- Aim for under about 30 seconds and a few MB once it's a GIF.
- Hand the raw recording over as a file. It's trimmed, scaled and checked frame by frame before it goes in `docs/media/`.

**After recording:** set the knob back to 360 and check drift (runbook, "Sync cadence knob").

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
