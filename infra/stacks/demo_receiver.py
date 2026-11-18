"""The demo receiver: one Lambda behind an HTTP API, a small table, SSM secrets.

Receiver data is disposable, so its table is destroyed with the stack. (Token Keeper's
own table and KMS key are RETAIN; that stack arrives in a later slice.)
"""

from __future__ import annotations

from typing import Any

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from receiver.store import TTL_ATTRIBUTE

SSM_PREFIX = "/token-keeper/demo/receiver"
SECRET_PARAMS = {
    "WEBHOOK_SECRET_PARAM": f"{SSM_PREFIX}/webhook-secret",
    "CONNECT_LINK_SECRET_PARAM": f"{SSM_PREFIX}/connect-link-secret",
    "DEMO_PASSCODE_PARAM": f"{SSM_PREFIX}/demo-passcode",
}
# Whole-API limits, plus a stricter one on the passcode form against guessing.
DEFAULT_THROTTLE = {"rate": 5, "burst": 10}
CONNECT_THROTTLE = {"rate": 1, "burst": 2}


class DemoReceiverStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        code: lambda_.Code,
        token_keeper_connect_url: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        table = dynamodb.Table(
            self,
            "Table",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute=TTL_ATTRIBUTE,
            removal_policy=RemovalPolicy.DESTROY,
        )

        api = apigw.HttpApi(self, "Api", create_default_stage=False)
        stage = apigw.HttpStage(
            self,
            "DefaultStage",
            http_api=api,
            stage_name="$default",
            auto_deploy=True,
            throttle=apigw.ThrottleSettings(
                rate_limit=DEFAULT_THROTTLE["rate"], burst_limit=DEFAULT_THROTTLE["burst"]
            ),
        )
        cfn_stage = stage.node.default_child
        assert isinstance(cfn_stage, apigw.CfnStage)
        cfn_stage.add_property_override(
            "RouteSettings",
            {
                "POST /connect": {
                    "ThrottlingRateLimit": CONNECT_THROTTLE["rate"],
                    "ThrottlingBurstLimit": CONNECT_THROTTLE["burst"],
                }
            },
        )

        log_group = logs.LogGroup(
            self,
            "HandlerLogs",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        fn = lambda_.Function(
            self,
            "Handler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="receiver.handler.handler",
            code=code,
            memory_size=256,
            timeout=Duration.seconds(10),
            log_group=log_group,
            environment={
                "RECEIVER_TABLE": table.table_name,
                "TOKEN_KEEPER_CONNECT_URL": token_keeper_connect_url,
                "RECEIVER_PUBLIC_URL": f"{api.api_endpoint}/",
                **SECRET_PARAMS,
            },
        )
        table.grant_read_write_data(fn)
        for env_name, param_name in SECRET_PARAMS.items():
            # SecureString values are created by hand before the first deploy (slice 4).
            ssm.StringParameter.from_secure_string_parameter_attributes(
                self, env_name, parameter_name=param_name
            ).grant_read(fn)

        integration = integrations.HttpLambdaIntegration("Integration", fn)
        for method, path in (
            (apigw.HttpMethod.GET, "/"),
            (apigw.HttpMethod.POST, "/webhooks/token-keeper"),
            (apigw.HttpMethod.POST, "/connect"),
        ):
            api.add_routes(path=path, methods=[method], integration=integration)

        CfnOutput(self, "ReceiverUrl", value=f"{api.api_endpoint}/")
        CfnOutput(self, "WebhookUrl", value=f"{api.api_endpoint}/webhooks/token-keeper")
