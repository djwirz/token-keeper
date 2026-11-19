"""The demo receiver: one Lambda behind an HTTP API, a small table, SSM secrets.

Receiver data is disposable, so its table is destroyed with the stack. (Token Keeper's
own table and KMS key are RETAIN.)

It publishes its URL to SSM for Token Keeper and reads Token Keeper's connect URL from
SSM at cold start (ADR 0014), so neither stack references the other.
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

from infra.stacks.shared import CONNECT_URL_PARAM, RECEIVER_URL_PARAM

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
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        table = dynamodb.Table(
            self,
            "Table",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
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
                "TOKEN_KEEPER_CONNECT_URL_PARAM": CONNECT_URL_PARAM,
                "RECEIVER_PUBLIC_URL": f"{api.api_endpoint}/",
                **SECRET_PARAMS,
            },
        )
        # Exactly what the handler does: one conditional PutItem per event, and a Scan.
        table.grant(fn, "dynamodb:PutItem", "dynamodb:Scan")
        for env_name, param_name in SECRET_PARAMS.items():
            # SecureString values are created by hand before the first deploy (slice 4).
            # Generate the passcode at random (e.g. secrets.token_urlsafe(16)): the
            # throttle on POST /connect slows guessing but doesn't stop it.
            ssm.StringParameter.from_secure_string_parameter_attributes(
                self, env_name, parameter_name=param_name
            ).grant_read(fn)

        ssm.StringParameter.from_string_parameter_name(
            self, "ConnectUrl", CONNECT_URL_PARAM
        ).grant_read(fn)
        ssm.StringParameter(
            self,
            "ReceiverUrlParam",
            parameter_name=RECEIVER_URL_PARAM,
            string_value=f"{api.api_endpoint}/",
            description="token-keeper: the demo receiver's base URL, read by Token Keeper",
        )

        integration = integrations.HttpLambdaIntegration("Integration", fn)
        for method, path in (
            (apigw.HttpMethod.GET, "/"),
            (apigw.HttpMethod.POST, "/webhooks/token-keeper"),
            (apigw.HttpMethod.POST, "/connect"),
        ):
            routes = api.add_routes(path=path, methods=[method], integration=integration)
            if path == "/connect":
                # The stage's RouteSettings names this route, so CloudFormation must
                # create the route first or the stage fails ("Unable to find Route").
                stage.node.add_dependency(*routes)

        CfnOutput(self, "ReceiverUrl", value=f"{api.api_endpoint}/")
        CfnOutput(self, "WebhookUrl", value=f"{api.api_endpoint}/webhooks/token-keeper")
