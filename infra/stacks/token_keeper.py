"""Token Keeper: its Lambdas (connect, callback, planner, worker, deliverer,
github-webhook) and data.

Synth only until slice 4. Data that must survive any deploy is RETAIN (spec §10): the
table (a replacement would lose every token) and the KMS key (deleting it would make
every stored token unreadable).

KMS key rotation is off for now: each rotated key version adds about a dollar a month
against the $5 budget (spec §3). Turning it on later is a one-line change.

One role per Lambda, granted exactly what its handler does (spec §9, ADR 0003):
- connect: ``kms:Encrypt`` (the PKCE verifier) and ``PutItem`` (the state record).
- callback: ``kms:Encrypt`` and ``kms:Decrypt``, the item writes its transaction uses,
  and ``SendMessage`` to the delivery queue.
- deliverer: ``Query`` (account + snapshot) and the queue; no KMS at all.
- planner: ``Query`` on the ``due`` index only, ``UpdateItem`` and ``SendMessage``
  to the sync queue; no KMS. Its ``UpdateItem`` is limited to the attributes its code
  touches (``dynamodb:Attributes``) and returns nothing (``dynamodb:ReturnValues``).
- worker: ``kms:Encrypt`` and ``kms:Decrypt`` (the only other role with Decrypt),
  ``UpdateItem``/``PutItem`` (lease, tokens, snapshot) and ``SendMessage`` to the
  delivery queue.
- github-webhook: ``GetItem`` on ``GHUSER#`` links, ``UpdateItem`` on ``ACCOUNT#``
  rows (the revocation takeover) and ``SendMessage`` to the delivery queue; no KMS
  (ADR 0018).

Table grants are written as explicit statements on the table ARN, because
``table.grant()`` also grants every index once the table has one. Only the planner
reads the index.

Item-level narrowing (ADR 0017): every role that touches only one kind of item gets a
``dynamodb:LeadingKeys`` condition on its partition-key prefix: connect ``STATE#``;
planner, worker and deliverer ``ACCOUNT#``; github-webhook ``GHUSER#`` for its read
and ``ACCOUNT#`` for its write, as two statements (ADR 0018). The callback writes all
four prefixes, so a condition would exclude nothing and it has none. The planner's
index ``Query`` has none either: there the leading key is ``state``.

The worker's timeout (40 s) is below its 60 s lease, so it can't outlive the lease
(ADR 0004, ADR 0015). Both queue consumers take one message per invocation.

It publishes its connect URL to SSM for the receiver and reads the receiver's URL and
the GitHub client ID from SSM at cold start (ADR 0014): no cross-stack reference.

The deliverer takes one message per invocation. Its 15 s timeout then always covers
one worst-case POST (2 s connect + 5 s read + signing), so a slow receiver can't time
out a whole batch and skip the backoff (PR 3 review).

Observability (spec §8, ADR 0019): every Lambda has X-Ray active tracing (its own
segment; no SDK) and prints EMF metrics to its log, so no role needs
``cloudwatch:PutMetricData``. Three alarms notify the ``token-keeper-alarms`` topic on
ALARM and on OK. The hand-deployed ``Alerts`` stack owns that topic and its email
subscription; this stack only builds the topic's ARN from its name. One dashboard shows
the metrics, the queues and each Lambda's errors.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cloudwatch_actions
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as event_sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from infra.stacks.shared import (
    ALARM_TOPIC_NAME,
    CONNECT_URL_PARAM,
    GITHUB_CLIENT_ID_PARAM,
    RECEIVER_URL_PARAM,
)
from token_keeper import metrics
from token_keeper.handlers.deliverer import MAX_RECEIVES
from token_keeper.handlers.worker import MAX_RECEIVES as SYNC_MAX_RECEIVES
from token_keeper.log import SERVICE
from token_keeper.store import DUE_INDEX, LEASE_SECONDS, TTL_ATTRIBUTE

SSM_PREFIX = "/token-keeper/demo/token-keeper"
SECRET_PARAMS = {
    "GITHUB_CLIENT_SECRET_PARAM": f"{SSM_PREFIX}/github-client-secret",
    "CONNECT_LINK_SECRET_PARAM": f"{SSM_PREFIX}/connect-link-secret",
    "WEBHOOK_SECRET_PARAM": f"{SSM_PREFIX}/webhook-secret",
    # The GitHub App's webhook secret, for inbound deliveries (slice 5, ADR 0018).
    "GITHUB_WEBHOOK_SECRET_PARAM": f"{SSM_PREFIX}/github-webhook-secret",
}
# The demo cadence knob (ADR 0020): a plain String TokenKeeper creates at the normal six
# hours. It is set by hand only while recording the README GIF, then put back.
SYNC_INTERVAL_PARAM = f"{SSM_PREFIX}/sync-interval-minutes"
SYNC_INTERVAL_DEFAULT_MINUTES = "360"
THROTTLE = {"rate": 5, "burst": 10}
DELIVERER_TIMEOUT_SECONDS = 15
DELIVERER_BATCH_SIZE = 1
WORKER_TIMEOUT_SECONDS = 40
WORKER_BATCH_SIZE = 1
PLANNER_TIMEOUT_SECONDS = 60
PLANNER_RATE_MINUTES = 15
# GitHub records a delivery as failed after 10 s, so answer well inside that.
GITHUB_WEBHOOK_TIMEOUT_SECONDS = 8
GITHUB_WEBHOOK_PATH = "/github/webhook"
# Partition-key prefixes (ADR 0013) for dynamodb:LeadingKeys (ADR 0017).
ACCOUNT_PREFIX = "ACCOUNT#"
STATE_PREFIX = "STATE#"
GHUSER_PREFIX = "GHUSER#"
# What the planner's push_due names: its keys, the two attributes it sets, and the
# state its condition reads. Nothing else, so never a token, expiry or lease field.
PLANNER_ATTRIBUTES = ("PK", "SK", "next_due_at", "version", "state")
# Alarms (spec §8, ADR 0019).
ALARM_PERIOD = Duration.minutes(5)
# RefreshFailed: one or more in any of the last 12 five-minute periods, i.e. in an hour.
# Short periods fire within minutes, where one hour-long period would wait for the hour.
REFRESH_FAILED_PERIODS = 12
# The planner runs every 15 minutes: three empty 15-minute periods is 45 minutes without
# a run, and three-of-three tolerates a run landing late in its period.
PLANNER_MISSING_PERIODS = 3
if WORKER_TIMEOUT_SECONDS >= LEASE_SECONDS:  # not an assert: python -O strips those
    raise ValueError("a worker must not outlive its lease (ADR 0015)")


def leading_keys(prefix: str) -> dict[str, dict[str, Any]]:
    """Every partition key the request touches starts with ``prefix``."""
    return {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [f"{prefix}*"]}}


class TokenKeeperStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        code: lambda_.Code,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        table = dynamodb.Table(
            self,
            "Table",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute=TTL_ATTRIBUTE,
            removal_policy=RemovalPolicy.RETAIN,
        )
        # The planner's index (spec §4). Keys only: no ciphertext is copied into it.
        table.add_global_secondary_index(
            index_name=DUE_INDEX,
            partition_key=dynamodb.Attribute(name="state", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="next_due_at", type=dynamodb.AttributeType.NUMBER),
            projection_type=dynamodb.ProjectionType.KEYS_ONLY,
        )
        key = kms.Key(
            self,
            "TokenKey",
            description="token-keeper: field-level encryption of OAuth tokens (ADR 0003)",
            enable_key_rotation=False,
            removal_policy=RemovalPolicy.RETAIN,
        )
        dlq = sqs.Queue(self, "DeliveryDlq", retention_period=Duration.days(14))
        queue = sqs.Queue(
            self,
            "DeliveryQueue",
            # AWS recommends at least six times the consumer's timeout.
            visibility_timeout=Duration.seconds(6 * DELIVERER_TIMEOUT_SECONDS),
            retention_period=Duration.days(4),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=MAX_RECEIVES, queue=dlq),
        )
        sync_dlq = sqs.Queue(self, "SyncDlq", retention_period=Duration.days(14))
        sync_queue = sqs.Queue(
            self,
            "SyncQueue",
            visibility_timeout=Duration.seconds(6 * WORKER_TIMEOUT_SECONDS),
            retention_period=Duration.days(1),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=SYNC_MAX_RECEIVES, queue=sync_dlq
            ),
        )

        def grant_table(
            fn: lambda_.Function,
            *actions: str,
            conditions: dict[str, dict[str, Any]] | None = None,
        ) -> None:
            # Table only, never its indexes (see the module docstring).
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=list(actions), resources=[table.table_arn], conditions=conditions
                )
            )

        api = apigw.HttpApi(self, "Api", create_default_stage=False)
        apigw.HttpStage(
            self,
            "DefaultStage",
            http_api=api,
            stage_name="$default",
            auto_deploy=True,
            throttle=apigw.ThrottleSettings(
                rate_limit=THROTTLE["rate"], burst_limit=THROTTLE["burst"]
            ),
        )

        secret_params: dict[str, ssm.IStringParameter] = {}

        def secret_param(env_name: str) -> ssm.IStringParameter:
            # SecureString values are created by hand before the first deploy (slice 4).
            # One construct per parameter, however many Lambdas read it.
            if env_name not in secret_params:
                secret_params[env_name] = (
                    ssm.StringParameter.from_secure_string_parameter_attributes(
                        self, env_name, parameter_name=SECRET_PARAMS[env_name]
                    )
                )
            return secret_params[env_name]

        receiver_url = ssm.StringParameter.from_string_parameter_name(
            self, "ReceiverUrl", RECEIVER_URL_PARAM
        )
        github_client_id = ssm.StringParameter.from_string_parameter_name(
            self, "GitHubClientId", GITHUB_CLIENT_ID_PARAM
        )

        def function(
            name: str, module: str, timeout: int, environment: dict[str, str]
        ) -> lambda_.Function:
            log_group = logs.LogGroup(
                self,
                f"{name}Logs",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=RemovalPolicy.DESTROY,
            )
            return lambda_.Function(
                self,
                name,
                runtime=lambda_.Runtime.PYTHON_3_12,
                architecture=lambda_.Architecture.ARM_64,
                handler=f"token_keeper.handlers.{module}.handler",
                code=code,
                memory_size=256,
                timeout=Duration.seconds(timeout),
                log_group=log_group,
                # Each Lambda's own segment, from the runtime: no X-Ray SDK (ADR 0019).
                tracing=lambda_.Tracing.ACTIVE,
                environment={
                    "TOKEN_KEEPER_TABLE": table.table_name,
                    "POWERTOOLS_SERVICE_NAME": SERVICE,
                    "POWERTOOLS_METRICS_NAMESPACE": metrics.NAMESPACE,
                    **environment,
                },
            )

        public_url = api.api_endpoint
        connect = function(
            "Connect",
            "connect",
            10,
            {
                "KMS_KEY_ID": key.key_arn,
                "TOKEN_KEEPER_PUBLIC_URL": public_url,
                "GITHUB_CLIENT_ID_PARAM": GITHUB_CLIENT_ID_PARAM,
                "RECEIVER_URL_PARAM": RECEIVER_URL_PARAM,
                "CONNECT_LINK_SECRET_PARAM": SECRET_PARAMS["CONNECT_LINK_SECRET_PARAM"],
            },
        )
        key.grant(connect, "kms:Encrypt")
        grant_table(connect, "dynamodb:PutItem", conditions=leading_keys(STATE_PREFIX))
        secret_param("CONNECT_LINK_SECRET_PARAM").grant_read(connect)
        github_client_id.grant_read(connect)
        receiver_url.grant_read(connect)

        callback = function(
            "Callback",
            "callback",
            15,
            {
                "KMS_KEY_ID": key.key_arn,
                "TOKEN_KEEPER_PUBLIC_URL": public_url,
                "GITHUB_CLIENT_ID_PARAM": GITHUB_CLIENT_ID_PARAM,
                "DELIVERY_QUEUE_URL": queue.queue_url,
                "SYNC_QUEUE_URL": sync_queue.queue_url,
                "GITHUB_CLIENT_SECRET_PARAM": SECRET_PARAMS["GITHUB_CLIENT_SECRET_PARAM"],
            },
        )
        key.grant(callback, "kms:Encrypt", "kms:Decrypt")
        grant_table(
            callback,
            "dynamodb:GetItem",
            "dynamodb:DeleteItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
        )
        queue.grant_send_messages(callback)
        sync_queue.grant_send_messages(callback)
        secret_param("GITHUB_CLIENT_SECRET_PARAM").grant_read(callback)
        github_client_id.grant_read(callback)

        deliverer = function(
            "Deliverer",
            "deliverer",
            DELIVERER_TIMEOUT_SECONDS,
            {
                "DELIVERY_QUEUE_URL": queue.queue_url,
                "RECEIVER_URL_PARAM": RECEIVER_URL_PARAM,
                "WEBHOOK_SECRET_PARAM": SECRET_PARAMS["WEBHOOK_SECRET_PARAM"],
            },
        )
        grant_table(deliverer, "dynamodb:Query", conditions=leading_keys(ACCOUNT_PREFIX))
        secret_param("WEBHOOK_SECRET_PARAM").grant_read(deliverer)
        receiver_url.grant_read(deliverer)
        deliverer.add_event_source(
            event_sources.SqsEventSource(
                queue, batch_size=DELIVERER_BATCH_SIZE, report_batch_item_failures=True
            )
        )

        planner = function(
            "Planner",
            "planner",
            PLANNER_TIMEOUT_SECONDS,
            {"SYNC_QUEUE_URL": sync_queue.queue_url},
        )
        planner.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:Query"], resources=[f"{table.table_arn}/index/{DUE_INDEX}"]
            )
        )
        grant_table(
            planner,
            "dynamodb:UpdateItem",
            conditions={
                **leading_keys(ACCOUNT_PREFIX),
                "ForAllValues:StringEquals": {"dynamodb:Attributes": list(PLANNER_ATTRIBUTES)},
                "StringEqualsIfExists": {"dynamodb:ReturnValues": "NONE"},
            },
        )
        sync_queue.grant_send_messages(planner)
        scheduler.Schedule(
            self,
            "PlannerSchedule",
            schedule=scheduler.ScheduleExpression.rate(Duration.minutes(PLANNER_RATE_MINUTES)),
            target=scheduler_targets.LambdaInvoke(planner, retry_attempts=0),
            description="token-keeper: find due accounts and enqueue syncs (spec §4)",
        )

        worker = function(
            "Worker",
            "worker",
            WORKER_TIMEOUT_SECONDS,
            {
                "KMS_KEY_ID": key.key_arn,
                "DELIVERY_QUEUE_URL": queue.queue_url,
                "GITHUB_CLIENT_ID_PARAM": GITHUB_CLIENT_ID_PARAM,
                "GITHUB_CLIENT_SECRET_PARAM": SECRET_PARAMS["GITHUB_CLIENT_SECRET_PARAM"],
                "SYNC_INTERVAL_MINUTES_PARAM": SYNC_INTERVAL_PARAM,
            },
        )
        ssm.StringParameter(
            self,
            "SyncIntervalParam",
            parameter_name=SYNC_INTERVAL_PARAM,
            string_value=SYNC_INTERVAL_DEFAULT_MINUTES,
            description=(
                "token-keeper: demo cadence knob, minutes between syncs (15-360). "
                "Set by hand only to record the README GIF, then back to 360 (ADR 0020)"
            ),
        ).grant_read(worker)
        key.grant(worker, "kms:Encrypt", "kms:Decrypt")
        grant_table(
            worker,
            "dynamodb:UpdateItem",
            "dynamodb:PutItem",
            conditions=leading_keys(ACCOUNT_PREFIX),
        )
        queue.grant_send_messages(worker)
        secret_param("GITHUB_CLIENT_SECRET_PARAM").grant_read(worker)
        github_client_id.grant_read(worker)
        worker.add_event_source(
            event_sources.SqsEventSource(
                sync_queue, batch_size=WORKER_BATCH_SIZE, report_batch_item_failures=True
            )
        )

        # The revocation webhook (spec §6.4, ADR 0010, ADR 0018): no KMS. It reads only
        # GitHub links and writes only Account rows (the takeover to needs_reauth).
        github_webhook = function(
            "GitHubWebhook",
            "github_webhook",
            GITHUB_WEBHOOK_TIMEOUT_SECONDS,
            {
                "DELIVERY_QUEUE_URL": queue.queue_url,
                "GITHUB_WEBHOOK_SECRET_PARAM": SECRET_PARAMS["GITHUB_WEBHOOK_SECRET_PARAM"],
            },
        )
        grant_table(github_webhook, "dynamodb:GetItem", conditions=leading_keys(GHUSER_PREFIX))
        grant_table(github_webhook, "dynamodb:UpdateItem", conditions=leading_keys(ACCOUNT_PREFIX))
        queue.grant_send_messages(github_webhook)
        secret_param("GITHUB_WEBHOOK_SECRET_PARAM").grant_read(github_webhook)
        api.add_routes(
            path=GITHUB_WEBHOOK_PATH,
            methods=[apigw.HttpMethod.POST],
            integration=integrations.HttpLambdaIntegration(
                "GitHubWebhookIntegration", github_webhook
            ),
        )

        for path, fn in (("/connect", connect), ("/callback", callback)):
            api.add_routes(
                path=path,
                methods=[apigw.HttpMethod.GET],
                integration=integrations.HttpLambdaIntegration(f"{fn.node.id}Integration", fn),
            )

        ssm.StringParameter(
            self,
            "ConnectUrlParam",
            parameter_name=CONNECT_URL_PARAM,
            string_value=f"{public_url}/connect",
            description="token-keeper: Token Keeper's /connect URL, read by the receiver",
        )
        self._observability(
            functions={
                "Connect": connect,
                "Callback": callback,
                "GitHubWebhook": github_webhook,
                "Planner": planner,
                "Worker": worker,
                "Deliverer": deliverer,
            },
            queues={"Sync": sync_queue, "Delivery": queue},
            dlqs={"Sync": sync_dlq, "Delivery": dlq},
        )

        CfnOutput(self, "ConnectUrl", value=f"{public_url}/connect")
        CfnOutput(self, "CallbackUrl", value=f"{public_url}/callback")
        CfnOutput(self, "GitHubWebhookUrl", value=f"{public_url}{GITHUB_WEBHOOK_PATH}")

    def _observability(
        self,
        *,
        functions: dict[str, lambda_.Function],
        queues: dict[str, sqs.Queue],
        dlqs: dict[str, sqs.Queue],
    ) -> None:
        """The three alarms and the dashboard (spec §8, ADR 0019)."""
        topic = sns.Topic.from_topic_arn(
            self,
            "AlarmTopic",
            self.format_arn(service="sns", resource=ALARM_TOPIC_NAME),
        )
        action = cloudwatch_actions.SnsAction(topic)

        def app_metric(name: str, statistic: str = "Sum") -> cloudwatch.Metric:
            return cloudwatch.Metric(
                namespace=metrics.NAMESPACE,
                metric_name=name,
                dimensions_map={"service": SERVICE},
                statistic=statistic,
                period=ALARM_PERIOD,
            )

        def visible(queue: sqs.Queue) -> cloudwatch.Metric:
            return queue.metric_approximate_number_of_messages_visible(
                statistic="Maximum", period=ALARM_PERIOD
            )

        # 1. Either DLQ holds a message. FILL: a quiet queue can stop reporting.
        dlq_alarm = cloudwatch.Alarm(
            self,
            "DlqMessagesAlarm",
            alarm_name="token-keeper-dlq-messages",
            alarm_description=(
                "A dead-letter queue holds a message. Runbook: docs/runbook.md#dlq-messages"
            ),
            metric=cloudwatch.MathExpression(
                expression="MAX([FILL(sync, 0), FILL(delivery, 0)])",
                using_metrics={
                    "sync": visible(dlqs["Sync"]),
                    "delivery": visible(dlqs["Delivery"]),
                },
                label="Messages in the DLQs",
                period=ALARM_PERIOD,
            ),
            threshold=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        # 2. GitHub rejected a refresh in the last hour.
        refresh_alarm = cloudwatch.Alarm(
            self,
            "RefreshFailedAlarm",
            alarm_name="token-keeper-refresh-failed",
            alarm_description=(
                "GitHub rejected a token refresh in the last hour. "
                "Runbook: docs/runbook.md#refresh-failed"
            ),
            metric=app_metric(metrics.REFRESH_FAILED),
            threshold=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            evaluation_periods=REFRESH_FAILED_PERIODS,
            datapoints_to_alarm=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        # 3. The planner hasn't run for 45 minutes. No data at all is the failure itself.
        planner_alarm = cloudwatch.Alarm(
            self,
            "PlannerMissingAlarm",
            alarm_name="token-keeper-planner-missing",
            alarm_description=(
                "The planner hasn't run for 45 minutes. Runbook: docs/runbook.md#planner-missing"
            ),
            metric=functions["Planner"].metric_invocations(
                statistic="Sum", period=Duration.minutes(15)
            ),
            threshold=1,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            evaluation_periods=PLANNER_MISSING_PERIODS,
            datapoints_to_alarm=PLANNER_MISSING_PERIODS,
            treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
        )
        alarms = (dlq_alarm, refresh_alarm, planner_alarm)
        for alarm in alarms:
            # OK too: the email that says it was put back.
            alarm.add_alarm_action(action)
            alarm.add_ok_action(action)

        def graph(title: str, *left: cloudwatch.IMetric, width: int = 8) -> cloudwatch.GraphWidget:
            return cloudwatch.GraphWidget(title=title, left=list(left), width=width, height=6)

        cloudwatch.Dashboard(
            self,
            "Dashboard",
            dashboard_name="token-keeper",
            default_interval=Duration.days(1),
            widgets=[
                [cloudwatch.AlarmStatusWidget(title="Alarms", alarms=list(alarms), width=24)],
                [
                    graph(
                        "Refreshes",
                        app_metric(metrics.REFRESH_SUCCEEDED),
                        app_metric(metrics.REFRESH_FAILED),
                    ),
                    graph(
                        "Syncs",
                        app_metric(metrics.SYNC_SUCCEEDED),
                        app_metric(metrics.SYNC_FAILED),
                        app_metric(metrics.ACCOUNTS_DUE),
                    ),
                    graph(
                        "Deliveries",
                        app_metric(metrics.DELIVERY_SUCCEEDED),
                        app_metric(metrics.DELIVERY_FAILED),
                    ),
                ],
                [
                    graph(
                        "Delivery latency (ms)",
                        app_metric(metrics.DELIVERY_LATENCY, "p50"),
                        app_metric(metrics.DELIVERY_LATENCY, "Maximum"),
                    ),
                    graph(
                        "Rejections and revocations",
                        app_metric(metrics.CONNECT_REJECTED),
                        app_metric(metrics.GITHUB_WEBHOOK_REJECTED),
                        app_metric(metrics.REVOCATION_RECEIVED),
                    ),
                    graph(
                        "Queue depth (visible)",
                        *(visible(q).with_(label=f"{name} queue") for name, q in queues.items()),
                        *(visible(q).with_(label=f"{name} DLQ") for name, q in dlqs.items()),
                    ),
                ],
                [
                    graph(
                        "Lambda errors",
                        *(
                            fn.metric_errors(statistic="Sum", period=ALARM_PERIOD).with_(label=name)
                            for name, fn in functions.items()
                        ),
                        width=24,
                    )
                ],
            ],
        )
