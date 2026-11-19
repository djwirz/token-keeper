"""Token Keeper: the connect, callback and deliverer Lambdas and the data they hold.

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
"""

from __future__ import annotations

from typing import Any

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as event_sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from token_keeper.handlers.deliverer import MAX_RECEIVES
from token_keeper.store import TTL_ATTRIBUTE

SSM_PREFIX = "/token-keeper/demo/token-keeper"
SECRET_PARAMS = {
    "GITHUB_CLIENT_SECRET_PARAM": f"{SSM_PREFIX}/github-client-secret",
    "CONNECT_LINK_SECRET_PARAM": f"{SSM_PREFIX}/connect-link-secret",
    "WEBHOOK_SECRET_PARAM": f"{SSM_PREFIX}/webhook-secret",
}
THROTTLE = {"rate": 5, "burst": 10}
DELIVERER_TIMEOUT_SECONDS = 15


class TokenKeeperStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        code: lambda_.Code,
        receiver_url: str,
        github_client_id: str,
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

        def secret_param(env_name: str) -> ssm.IStringParameter:
            # SecureString values are created by hand before the first deploy (slice 4).
            return ssm.StringParameter.from_secure_string_parameter_attributes(
                self, env_name, parameter_name=SECRET_PARAMS[env_name]
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
                "GITHUB_CLIENT_ID": github_client_id,
                "ALLOWED_RETURN_TO": self.to_json_string({"demo": [receiver_url]}),
                "CONNECT_LINK_SECRET_PARAM": SECRET_PARAMS["CONNECT_LINK_SECRET_PARAM"],
            },
        )
        key.grant(connect, "kms:Encrypt")
        table.grant(connect, "dynamodb:PutItem")
        secret_param("CONNECT_LINK_SECRET_PARAM").grant_read(connect)

        callback = function(
            "Callback",
            "callback",
            15,
            {
                "KMS_KEY_ID": key.key_arn,
                "TOKEN_KEEPER_PUBLIC_URL": public_url,
                "GITHUB_CLIENT_ID": github_client_id,
                "DELIVERY_QUEUE_URL": queue.queue_url,
                "GITHUB_CLIENT_SECRET_PARAM": SECRET_PARAMS["GITHUB_CLIENT_SECRET_PARAM"],
            },
        )
        key.grant(callback, "kms:Encrypt", "kms:Decrypt")
        table.grant(
            callback,
            "dynamodb:GetItem",
            "dynamodb:DeleteItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:ConditionCheckItem",
        )
        queue.grant_send_messages(callback)
        secret_param("GITHUB_CLIENT_SECRET_PARAM").grant_read(callback)

        deliverer = function(
            "Deliverer",
            "deliverer",
            DELIVERER_TIMEOUT_SECONDS,
            {
                "DELIVERY_QUEUE_URL": queue.queue_url,
                "RECEIVER_WEBHOOK_URL": f"{receiver_url}webhooks/token-keeper",
                "WEBHOOK_SECRET_PARAM": SECRET_PARAMS["WEBHOOK_SECRET_PARAM"],
            },
        )
        table.grant(deliverer, "dynamodb:Query")
        secret_param("WEBHOOK_SECRET_PARAM").grant_read(deliverer)
        deliverer.add_event_source(
            event_sources.SqsEventSource(queue, batch_size=10, report_batch_item_failures=True)
        )

        for path, fn in (("/connect", connect), ("/callback", callback)):
            api.add_routes(
                path=path,
                methods=[apigw.HttpMethod.GET],
                integration=integrations.HttpLambdaIntegration(f"{fn.node.id}Integration", fn),
            )

        CfnOutput(self, "ConnectUrl", value=f"{public_url}/connect")
        CfnOutput(self, "CallbackUrl", value=f"{public_url}/callback")
