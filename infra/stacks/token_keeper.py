"""Token Keeper: its Lambdas (connect, callback, planner, worker, deliverer) and data.

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
- planner: ``Query`` on the ``due`` index only, ``UpdateItem`` (the push) and
  ``SendMessage`` to the sync queue; no KMS.
- worker: ``kms:Encrypt`` and ``kms:Decrypt`` (the only other role with Decrypt),
  ``UpdateItem``/``PutItem`` (lease, tokens, snapshot) and ``SendMessage`` to the
  delivery queue.

Table grants are written as explicit statements on the table ARN, because
``table.grant()`` also grants every index once the table has one. Only the planner
reads the index.

The worker's timeout (40 s) is below its 60 s lease, so it can't outlive the lease
(ADR 0004, ADR 0015). Both queue consumers take one message per invocation.

It publishes its connect URL to SSM for the receiver and reads the receiver's URL and
the GitHub client ID from SSM at cold start (ADR 0014): no cross-stack reference.

The deliverer takes one message per invocation. Its 15 s timeout then always covers
one worst-case POST (2 s connect + 5 s read + signing), so a slow receiver can't time
out a whole batch and skip the backoff (PR 3 review).
"""

from __future__ import annotations

from typing import Any

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as event_sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from infra.stacks.shared import CONNECT_URL_PARAM, GITHUB_CLIENT_ID_PARAM, RECEIVER_URL_PARAM
from token_keeper.handlers.deliverer import MAX_RECEIVES
from token_keeper.handlers.worker import MAX_RECEIVES as SYNC_MAX_RECEIVES
from token_keeper.store import DUE_INDEX, LEASE_SECONDS, TTL_ATTRIBUTE

SSM_PREFIX = "/token-keeper/demo/token-keeper"
SECRET_PARAMS = {
    "GITHUB_CLIENT_SECRET_PARAM": f"{SSM_PREFIX}/github-client-secret",
    "CONNECT_LINK_SECRET_PARAM": f"{SSM_PREFIX}/connect-link-secret",
    "WEBHOOK_SECRET_PARAM": f"{SSM_PREFIX}/webhook-secret",
}
THROTTLE = {"rate": 5, "burst": 10}
DELIVERER_TIMEOUT_SECONDS = 15
DELIVERER_BATCH_SIZE = 1
WORKER_TIMEOUT_SECONDS = 40
WORKER_BATCH_SIZE = 1
PLANNER_TIMEOUT_SECONDS = 60
PLANNER_RATE_MINUTES = 15
assert WORKER_TIMEOUT_SECONDS < LEASE_SECONDS, "a worker must not outlive its lease"


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

        def grant_table(fn: lambda_.Function, *actions: str) -> None:
            # Table only, never its indexes (see the module docstring).
            fn.add_to_role_policy(
                iam.PolicyStatement(actions=list(actions), resources=[table.table_arn])
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
                environment={
                    "TOKEN_KEEPER_TABLE": table.table_name,
                    "POWERTOOLS_SERVICE_NAME": "token-keeper",
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
        grant_table(connect, "dynamodb:PutItem")
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
        grant_table(deliverer, "dynamodb:Query")
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
        grant_table(planner, "dynamodb:UpdateItem")
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
            },
        )
        key.grant(worker, "kms:Encrypt", "kms:Decrypt")
        grant_table(worker, "dynamodb:UpdateItem", "dynamodb:PutItem")
        queue.grant_send_messages(worker)
        secret_param("GITHUB_CLIENT_SECRET_PARAM").grant_read(worker)
        github_client_id.grant_read(worker)
        worker.add_event_source(
            event_sources.SqsEventSource(
                sync_queue, batch_size=WORKER_BATCH_SIZE, report_batch_item_failures=True
            )
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
        CfnOutput(self, "ConnectUrl", value=f"{public_url}/connect")
        CfnOutput(self, "CallbackUrl", value=f"{public_url}/callback")
