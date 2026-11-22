# token-keeper runbook

What to do when an alarm email arrives, and how to delete an account's data on request. The alarms are described in spec §8 and [ADR 0019](adr/0019-observability-as-built.md). Every alarm emails on ALARM and again on OK, from the `token-keeper-alarms` topic in the hand-deployed `Alerts` stack.

Everything below assumes `AWS_PROFILE=token-keeper` (after `aws login --profile token-keeper`) and region `us-east-1`. Nothing here prints a token. Keep the account ID out of anything you paste elsewhere.

**Where to look first:**
- the CloudWatch dashboard `token-keeper`, which shows the three alarms, every metric, both queues, both DLQs and each Lambda's errors;
- the Lambda logs, where every line carries `account_id`, `correlation_id` and, for deliveries, `webhook_id`.

Finding resources by their logical-ID prefix (`Worker`, `SyncDlq`, `WorkerLogs`…):

```bash
res() { aws cloudformation list-stack-resources --stack-name TokenKeeper --query "StackResourceSummaries[?ResourceType=='$1' && starts_with(LogicalResourceId, '$2')].PhysicalResourceId" --output text; }
logs() { aws logs filter-log-events --log-group-name "$(res AWS::Logs::LogGroup "$1Logs")" --start-time $(( ($(date +%s) - ${3:-3600}) * 1000 )) --filter-pattern "$2" --query 'events[].[message]' --output text; }
```

`logs Worker '"sync.error"'` shows the last hour's matching worker lines. A third argument changes the window, in seconds.

## Is anyone listening?

An alarm emails no one if the subscription was never confirmed, or if someone clicked "unsubscribe" in an earlier alarm email. This check prints no address:

```bash
TOPIC=$(aws sns list-topics --query "Topics[?ends_with(TopicArn, ':token-keeper-alarms')].TopicArn" --output text)
aws sns list-subscriptions-by-topic --topic-arn "$TOPIC" --query 'Subscriptions[].[Protocol, SubscriptionArn != `PendingConfirmation`]' --output text
```

`email True` means confirmed. If nothing comes back, or `False`, redeploy `Alerts` (docs/deploy.md) and click the link in the new confirmation email.

## DLQ messages

**What it means.** A message reached a dead-letter queue, either `SyncDlq` or `DeliveryDlq`.
- **Sync DLQ:** the worker raised three times on one message. GitHub trouble never gets here, because it's recorded on the account (ADR 0015). So this is a bug, or AWS itself failing (DynamoDB, KMS).
- **Delivery DLQ:** an event failed for about a day: the receiver was down, or rejecting the signature, or the message is poison. For an `active` account the next sync heals the gap. For a `needs_reauth` account the event stays lost until it's redriven (spec §6.3).

**How to check.** Count and look, without deleting anything:

```bash
for q in SyncDlq DeliveryDlq; do echo "$q $(aws sqs get-queue-attributes --queue-url "$(res AWS::SQS::Queue $q)" --attribute-names ApproximateNumberOfMessages --query Attributes.ApproximateNumberOfMessages --output text)"; done
aws sqs receive-message --queue-url "$(res AWS::SQS::Queue SyncDlq)" --max-number-of-messages 10 --visibility-timeout 30 --attribute-names All --message-attribute-names All
```

The body is `{"account_id": …}` for a sync, or `{"account_id", "type", "webhook_id"}` for a delivery. Messages carry ids only, never a token or an event body. Find what failed:
- for a sync: `logs Worker '"sync.error"' 86400`, with the account id, then the traceback;
- for a delivery: `logs Deliverer '"delivery.failed"' 86400`, where `reason` is `http_500`, `http_401` (a webhook-secret mismatch) or `ConnectTimeout`.

**How to recover.** Fix the cause first: a code fix ships through CI; a secret mismatch is fixed with `put_demo_secrets.py` (docs/deploy.md). Then do one of these:

- **Redrive.** This moves every message in a DLQ back to its work queue (`DeliveryDlq` → `DeliveryQueue`, `SyncDlq` → `SyncQueue`). Deliveries are safe to redrive: the body is built from the account's state at send time, and the receiver ignores an equal or lower `version`. Syncs are safe too: the lease guards them, and an account that's no longer `active` is skipped.
  ```bash
  arn() { aws sqs get-queue-attributes --queue-url "$(res AWS::SQS::Queue "$1")" --attribute-names QueueArn --query Attributes.QueueArn --output text; }
  aws sqs start-message-move-task --source-arn "$(arn DeliveryDlq)" --destination-arn "$(arn DeliveryQueue)"
  aws sqs list-message-move-tasks --source-arn "$(arn DeliveryDlq)" --query 'Results[0].[Status, ApproximateNumberOfMessagesMoved, FailureReason]'
  ```
  Always name the destination. Without `--destination-arn`, SQS sends each message back to the queue it dead-lettered from, and a message that was put on the DLQ directly (by hand, or by a drill) has no such queue: the whole task fails with `CouldNotDetermineMessageSource` and moves nothing. A moved message gets a new `SentTimestamp`, so a redriven delivery's `DeliveryLatency` counts from the redrive, not from the original event.
- **Delete a poison message.** Use this for a message that can never succeed, such as a body that isn't JSON. Look at the body first, then delete it with the receipt handle from that same receive:
  ```bash
  RECEIPT=$(aws sqs receive-message --queue-url "$(res AWS::SQS::Queue SyncDlq)" --visibility-timeout 60 --query 'Messages[0].ReceiptHandle' --output text)
  aws sqs delete-message --queue-url "$(res AWS::SQS::Queue SyncDlq)" --receipt-handle "$RECEIPT"
  ```

The alarm goes back to OK, with an email, usually within a quarter of an hour of the DLQ emptying. Don't take that email as proof the DLQ is empty: SQS can stop reporting a queue nobody touches, and a quiet DLQ then reads as zero. Check with `get-queue-attributes` (above).

## Refresh failed

**What it means.** GitHub rejected a refresh at least once in the last hour. Either it rejected the refresh token (`bad_refresh_token`), and that account is usually now `needs_reauth` (`refresh_rejected`), or it rejected *our* client ID or secret (`incorrect_client_credentials`, `unauthorized_client`). Transient token-endpoint errors never fire this (ADR 0019). The likely causes:
0. **Our client secret is wrong** (the log's `reason` is `token_error:incorrect_client_credentials` or `token_error:unauthorized_client`, usually on every account). Every refresh fails until it's fixed, but accounts stay `active` and keep their tokens. Fix the secret with `put_demo_secrets.py --overwrite` (docs/deploy.md) and force a cold start of the Worker. The next sync of each account then refreshes normally.
1. **A lost rotation** (spec §7). GitHub rotated the pair, but the response or the write was lost. Look for `token_unreachable` or `sync.error` on the same account shortly before.
2. **The rotation hazard** (spec §5). Two refreshes raced, or a stale token was used. The lease should make this impossible, so treat it as a bug.
3. **The grant really is gone:** revoked, and the revocation webhook missed it, or hadn't landed yet. A worker that refreshes in the seconds after a user revokes gets `bad_refresh_token` before the webhook's takeover. That's benign: check `logs GitHubWebhook '"github_webhook.revoked"' 7200` for a revocation within the same minute. If the takeover won the race, the account ends `needs_reauth` with reason `revoked`, not `refresh_rejected`.

**How to check.**

```bash
logs Worker '"refresh.failed"' 7200
```

Each line carries `account_id`, `correlation_id` and `reason`. Then read the account's earlier lines (`logs Worker '"acc_…"' 86400`) for the attempts before it:
- a `refresh.succeeded` whose final write then failed, or a `token_unreachable`, suggests a lost rotation;
- two `refresh.*` lines with different `correlation_id`s within the same minute is a race. Open an issue with both.

The account's item shows `state`, `state_reason` and `last_error_code`. It has no tokens left; those were deleted.

**How to recover.** Nothing can bring the old grant back: GitHub documents no grace period. The user reconnects from the main app, and the callback takes the account over with a fresh pair. The alarm clears by itself a little over an hour after the last rejection, once its twelve five-minute periods have aged out.

## Planner missing

**What it means.** The planner Lambda hasn't been invoked for three whole fifteen-minute periods in a row, so the email comes about an hour after its last run. No account is being picked up for a scheduled sync. Connects still work, and so does the callback's immediate sync, so the receiver's "last synced" times stop moving.

**How to check.**

```bash
SCHEDULE=$(res AWS::Scheduler::Schedule PlannerSchedule)
aws scheduler get-schedule --name "$SCHEDULE" --query '[State, ScheduleExpression, Target.Arn]'
logs Planner '"planner.enqueued"' 7200
```

- `DISABLED`: someone turned it off (the drill does exactly this).
- `ENABLED` but no invocations: look at the planner's own errors on the dashboard, its log, and the scheduler role.
- Invocations, but all erroring: that's not this alarm; see the Lambda errors widget.

**How to recover.** Re-enable the schedule with its *whole* definition. `update-schedule` replaces everything, and leaving a field out resets it:

```bash
DIR=$(mktemp -d)
aws scheduler get-schedule --name "$SCHEDULE" > "$DIR/schedule.json"
# Every field it returned, minus the three that are read-only.
jq 'del(.Arn, .CreationDate, .LastModificationDate) | .State = "ENABLED"' "$DIR/schedule.json" > "$DIR/enabled.json"
aws scheduler update-schedule --cli-input-json "file://$DIR/enabled.json"
```

Then confirm it matches the template. Start drift detection, and once it has finished, list anything that isn't in sync:
```bash
aws cloudformation detect-stack-drift --stack-name TokenKeeper
aws cloudformation describe-stack-resource-drifts --stack-name TokenKeeper --stack-resource-drift-status-filters MODIFIED DELETED
```

That should list nothing. If the schedule shows as `NOT_CHECKED` (CloudFormation's drift detection doesn't cover every resource type), compare `get-schedule` with the template instead: same expression, flexible window, target and retry policy, and `ENABLED`. The next run (within 15 minutes) logs `planner.enqueued`. Accounts that came due while it was off are all picked up on that run, since the due index has no window. The alarm clears within about fifteen minutes of that run: one period with an invocation breaks the three-of-three.

## Deleting an account's data on request

There's no disconnect API (spec §2). A user who wants their data gone asks, and an operator deletes it.

1. **Find the account.** It's in the main app's events (`account_id`), or in the table by the user link `LINK#<app_id>#<external_user_id>`.
2. **Ask the user to revoke the App on GitHub** (Settings → Applications → Authorized GitHub Apps), if they haven't. Deleting our copy deletes the tokens, but the grant itself lives at GitHub until the user revokes it. If they revoke first, the webhook moves the account to `needs_reauth`, which is fine: delete it anyway.
3. **Delete it:**
   ```bash
   uv run python scripts/account_ops.py --profile token-keeper delete acc_…
   ```
   This deletes the Account, its Snapshot, the user link and the GitHub link, in one transaction. A link that already points at another account is left alone. If the account changes while the script runs (a reconnect, a sync, a revocation), nothing is deleted and it says to run it again. It prints each key it deleted, and is harmless to run twice. A worker mid-sync can't bring anything back: its writes are conditional on its lease, which goes with the Account item. A delivery still queued finds no account and drops the message (`delivery.account_missing`).
4. **The main app's copy is the main app's to delete.** For the demo receiver, add `--receiver` to the same command, which deletes its row.
5. **Check:** the user and GitHub links no longer resolve, and the receiver page no longer shows the card.
   ```bash
   TABLE=$(res AWS::DynamoDB::Table Table)
   aws dynamodb get-item --table-name "$TABLE" --key '{"PK":{"S":"ACCOUNT#acc_…"},"SK":{"S":"META"}}'
   ```
   It should print nothing.

Logs keep `account_id` and `login` for two weeks, then expire (spec §8). Metrics carry no per-account data.
