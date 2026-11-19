"""CDK assertion tests: the infrastructure says what the spec says (spec §8-§10)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk import aws_lambda as lambda_
from aws_cdk.assertions import Match, Template

from infra.stacks.budget import BudgetStack
from infra.stacks.demo_receiver import SECRET_PARAMS, DemoReceiverStack
from receiver.store import KEY_SCHEMA

ENV = cdk.Environment(account="111111111111", region="us-east-1")


@pytest.fixture(scope="module")
def budget() -> Template:
    app = cdk.App()
    return Template.from_stack(BudgetStack(app, "Budget", email="ci@example.invalid", env=ENV))


@pytest.fixture(scope="module")
def receiver(tmp_path_factory: pytest.TempPathFactory) -> Template:
    code_dir: Path = tmp_path_factory.mktemp("code")
    (code_dir / "index.py").write_text("")
    app = cdk.App()
    stack = DemoReceiverStack(
        app,
        "DemoReceiver",
        code=lambda_.Code.from_asset(str(code_dir)),
        token_keeper_connect_url="https://token-keeper.invalid/connect",
        env=ENV,
    )
    return Template.from_stack(stack)


def test_budget_is_five_dollars_with_two_email_alerts(budget: Template) -> None:
    budget.has_resource_properties(
        "AWS::Budgets::Budget",
        {
            "Budget": Match.object_like(
                {"BudgetLimit": {"Amount": 5, "Unit": "USD"}, "TimeUnit": "MONTHLY"}
            ),
            "NotificationsWithSubscribers": [
                {
                    "Notification": Match.object_like({"Threshold": pct}),
                    "Subscribers": [{"SubscriptionType": "EMAIL", "Address": "ci@example.invalid"}],
                }
                for pct in (50, 100)
            ],
        },
    )


def test_secrets_reach_lambda_only_as_ssm_parameter_names(receiver: Template) -> None:
    # receiver.config prefers a plaintext env var over SSM, so one must never be set here.
    (function,) = receiver.find_resources("AWS::Lambda::Function").values()
    variables = function["Properties"]["Environment"]["Variables"]
    for param_env in SECRET_PARAMS:
        assert param_env.removesuffix("_PARAM") not in variables


def test_lambda_runtime_and_architecture(receiver: Template) -> None:
    receiver.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Runtime": "python3.12",
            "Architectures": ["arm64"],
            "Handler": "receiver.handler.handler",
            "Environment": {"Variables": Match.object_like(SECRET_PARAMS)},
        },
    )


def test_table_matches_local_schema_and_is_disposable(receiver: Template) -> None:
    receiver.has_resource(
        "AWS::DynamoDB::Table",
        {
            "Properties": Match.object_like({"KeySchema": KEY_SCHEMA}),
            "DeletionPolicy": "Delete",
        },
    )


def test_logs_kept_two_weeks(receiver: Template) -> None:
    receiver.has_resource_properties("AWS::Logs::LogGroup", {"RetentionInDays": 14})


def test_api_is_throttled_and_connect_more_so(receiver: Template) -> None:
    receiver.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        {
            "StageName": "$default",
            "DefaultRouteSettings": Match.object_like(
                {"ThrottlingRateLimit": 5, "ThrottlingBurstLimit": 10}
            ),
            "RouteSettings": {
                "POST /connect": {"ThrottlingRateLimit": 1, "ThrottlingBurstLimit": 2}
            },
        },
    )
    receiver.resource_count_is("AWS::ApiGatewayV2::Route", 3)


def test_stage_waits_for_the_route_its_settings_name(receiver: Template) -> None:
    # Without this dependency CloudFormation may create the stage first and fail on
    # RouteSettings["POST /connect"] ("Unable to find Route by key").
    routes = receiver.find_resources(
        "AWS::ApiGatewayV2::Route", {"Properties": {"RouteKey": "POST /connect"}}
    )
    (stage,) = receiver.find_resources("AWS::ApiGatewayV2::Stage").values()
    assert set(routes) <= set(stage.get("DependsOn", []))


def _flatten(value: Any) -> str:
    """Render a policy resource as one string, so a wildcard can't hide inside Fn::Join."""
    if isinstance(value, str):
        return value
    if "Fn::Join" in value:
        separator, parts = value["Fn::Join"]
        return str(separator).join(_flatten(p) for p in parts)
    if "Ref" in value:
        return "${" + str(value["Ref"]) + "}"
    if "Fn::GetAtt" in value:
        return "${" + ".".join(value["Fn::GetAtt"]) + "}"
    raise AssertionError(f"unexpected resource form: {value}")


def _grants(template: Template) -> set[tuple[str, str]]:
    """Every (action, resource) pair the stack's IAM policies allow."""
    pairs: set[tuple[str, str]] = set()
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            assert statement["Effect"] == "Allow"
            actions = statement["Action"]
            resources = statement["Resource"]
            for action in [actions] if isinstance(actions, str) else actions:
                for resource in resources if isinstance(resources, list) else [resources]:
                    pairs.add((action, _flatten(resource)))
    return pairs


def test_lambda_permissions_are_exactly_what_the_handler_needs(receiver: Template) -> None:
    # An exact match: any added action, broader resource or wildcard fails this test.
    (table_id,) = receiver.find_resources("AWS::DynamoDB::Table")
    table = "${" + table_id + ".Arn}"
    ssm_actions = (
        "ssm:DescribeParameters",
        "ssm:GetParameter",
        "ssm:GetParameterHistory",
        "ssm:GetParameters",
    )
    expected = {("dynamodb:PutItem", table), ("dynamodb:Scan", table)} | {
        (action, f"arn:${{AWS::Partition}}:ssm:{ENV.region}:{ENV.account}:parameter{name}")
        for action in ssm_actions
        for name in SECRET_PARAMS.values()
    }
    assert _grants(receiver) == expected
