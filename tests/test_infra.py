"""CDK assertion tests: the infrastructure says what the spec says (spec §8-§10)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk import aws_lambda as lambda_
from aws_cdk.assertions import Match, Template

from infra.stacks.budget import BudgetStack
from infra.stacks.demo_receiver import SECRET_PARAMS, DemoReceiverStack
from infra.stacks.shared import CONNECT_URL_PARAM, GITHUB_CLIENT_ID_PARAM, RECEIVER_URL_PARAM
from infra.stacks.token_keeper import DELIVERER_BATCH_SIZE, TokenKeeperStack
from infra.stacks.token_keeper import SECRET_PARAMS as TK_SECRET_PARAMS
from receiver.store import KEY_SCHEMA
from token_keeper.handlers.deliverer import MAX_RECEIVES
from token_keeper.store import ATTRIBUTE_DEFINITIONS as TK_ATTRIBUTE_DEFINITIONS
from token_keeper.store import KEY_SCHEMA as TK_KEY_SCHEMA
from token_keeper.store import TTL_ATTRIBUTE

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
        for name in [*SECRET_PARAMS.values(), CONNECT_URL_PARAM]
    }
    assert _grants(receiver) == expected


# Token Keeper ------------------------------------------------------------------


@pytest.fixture(scope="module")
def token_keeper(tmp_path_factory: pytest.TempPathFactory) -> Template:
    code_dir: Path = tmp_path_factory.mktemp("tk-code")
    (code_dir / "index.py").write_text("")
    app = cdk.App()
    stack = TokenKeeperStack(
        app,
        "TokenKeeper",
        code=lambda_.Code.from_asset(str(code_dir)),
        env=ENV,
    )
    return Template.from_stack(stack)


def _role_of(template: Template, handler: str) -> str:
    functions = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Handler": handler}}
    )
    (function,) = functions.values()
    role: str = function["Properties"]["Role"]["Fn::GetAtt"][0]
    return role


def _grants_by_role(template: Template) -> dict[str, set[tuple[str, str]]]:
    by_role: dict[str, set[tuple[str, str]]] = {}
    for policy in template.find_resources("AWS::IAM::Policy").values():
        (role,) = policy["Properties"]["Roles"]
        pairs = by_role.setdefault(role["Ref"], set())
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            assert statement["Effect"] == "Allow"
            actions = statement["Action"]
            resources = statement["Resource"]
            for action in [actions] if isinstance(actions, str) else actions:
                for resource in resources if isinstance(resources, list) else [resources]:
                    pairs.add((action, _flatten(resource)))
    return by_role


def test_table_and_key_are_retained(token_keeper: Template) -> None:
    for resource_type in ("AWS::DynamoDB::Table", "AWS::KMS::Key"):
        (resource,) = token_keeper.find_resources(resource_type).values()
        assert resource["DeletionPolicy"] == "Retain"
        assert resource["UpdateReplacePolicy"] == "Retain"


def test_table_matches_local_schema(token_keeper: Template) -> None:
    token_keeper.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "KeySchema": TK_KEY_SCHEMA,
            "AttributeDefinitions": TK_ATTRIBUTE_DEFINITIONS,
            "TimeToLiveSpecification": {"AttributeName": TTL_ATTRIBUTE, "Enabled": True},
            "BillingMode": "PAY_PER_REQUEST",
        },
    )


def test_only_the_callback_can_decrypt(token_keeper: Template) -> None:
    grants = _grants_by_role(token_keeper)
    callback = _role_of(token_keeper, "token_keeper.handlers.callback.handler")
    decrypters = {
        role for role, pairs in grants.items() if any(a == "kms:Decrypt" for a, _ in pairs)
    }
    assert decrypters == {callback}
    # And nothing anywhere grants kms:* or a wildcard action.
    for pairs in grants.values():
        assert not any(action.endswith("*") for action, _ in pairs)


def test_each_role_has_exactly_what_its_handler_needs(token_keeper: Template) -> None:
    (table_id,) = token_keeper.find_resources("AWS::DynamoDB::Table")
    (key_id,) = token_keeper.find_resources("AWS::KMS::Key")
    queues = token_keeper.find_resources(
        "AWS::SQS::Queue", {"Properties": {"RedrivePolicy": Match.any_value()}}
    )
    (queue_id,) = queues
    table, key, queue = f"${{{table_id}.Arn}}", f"${{{key_id}.Arn}}", f"${{{queue_id}.Arn}}"

    def ssm(name: str) -> set[tuple[str, str]]:
        arn = f"arn:${{AWS::Partition}}:ssm:{ENV.region}:{ENV.account}:parameter{name}"
        return {
            (action, arn)
            for action in (
                "ssm:DescribeParameters",
                "ssm:GetParameter",
                "ssm:GetParameterHistory",
                "ssm:GetParameters",
            )
        }

    expected = {
        "token_keeper.handlers.connect.handler": {("kms:Encrypt", key), ("dynamodb:PutItem", table)}
        | ssm(TK_SECRET_PARAMS["CONNECT_LINK_SECRET_PARAM"])
        | ssm(GITHUB_CLIENT_ID_PARAM)
        | ssm(RECEIVER_URL_PARAM),
        "token_keeper.handlers.callback.handler": {
            ("kms:Encrypt", key),
            ("kms:Decrypt", key),
            ("dynamodb:GetItem", table),
            ("dynamodb:DeleteItem", table),
            ("dynamodb:PutItem", table),
            ("dynamodb:UpdateItem", table),
            ("sqs:SendMessage", queue),
            ("sqs:GetQueueAttributes", queue),
            ("sqs:GetQueueUrl", queue),
        }
        | ssm(TK_SECRET_PARAMS["GITHUB_CLIENT_SECRET_PARAM"])
        | ssm(GITHUB_CLIENT_ID_PARAM),
        "token_keeper.handlers.deliverer.handler": {
            ("dynamodb:Query", table),
            ("sqs:ReceiveMessage", queue),
            ("sqs:DeleteMessage", queue),
            ("sqs:ChangeMessageVisibility", queue),
            ("sqs:GetQueueAttributes", queue),
            ("sqs:GetQueueUrl", queue),
        }
        | ssm(TK_SECRET_PARAMS["WEBHOOK_SECRET_PARAM"])
        | ssm(RECEIVER_URL_PARAM),
    }
    grants = _grants_by_role(token_keeper)
    assert set(grants) == {_role_of(token_keeper, h) for h in expected}
    for handler_path, pairs in expected.items():
        assert grants[_role_of(token_keeper, handler_path)] == pairs, handler_path


def test_delivery_queue_has_a_dlq_and_partial_batch_failures(token_keeper: Template) -> None:
    token_keeper.has_resource_properties(
        "AWS::SQS::Queue",
        {"RedrivePolicy": Match.object_like({"maxReceiveCount": MAX_RECEIVES})},
    )
    token_keeper.resource_count_is("AWS::SQS::Queue", 2)
    token_keeper.has_resource_properties(
        "AWS::Lambda::EventSourceMapping",
        {"FunctionResponseTypes": ["ReportBatchItemFailures"]},
    )


def test_token_keeper_secrets_are_ssm_names_and_logs_kept_two_weeks(
    token_keeper: Template,
) -> None:
    functions = token_keeper.find_resources("AWS::Lambda::Function")
    assert len(functions) == 3
    for function in functions.values():
        variables = function["Properties"]["Environment"]["Variables"]
        for param_env in TK_SECRET_PARAMS:
            assert param_env.removesuffix("_PARAM") not in variables
        assert function["Properties"]["Architectures"] == ["arm64"]
    log_groups = token_keeper.find_resources("AWS::Logs::LogGroup")
    assert len(log_groups) == 3
    assert all(g["Properties"]["RetentionInDays"] == 14 for g in log_groups.values())


def test_token_keeper_api_routes_and_throttle(token_keeper: Template) -> None:
    routes = token_keeper.find_resources("AWS::ApiGatewayV2::Route")
    assert {r["Properties"]["RouteKey"] for r in routes.values()} == {
        "GET /connect",
        "GET /callback",
    }
    token_keeper.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        {
            "DefaultRouteSettings": Match.object_like(
                {"ThrottlingRateLimit": 5, "ThrottlingBurstLimit": 10}
            )
        },
    )


def test_deliverer_timeout_covers_its_whole_batch(token_keeper: Template) -> None:
    # A batch that outlives the Lambda is killed before any backoff is set, and every
    # message burns a receive per visibility timeout (PR 3 review). Allow 10 s per
    # message: one POST is at most 2 s to connect plus 5 s to read.
    (mapping,) = token_keeper.find_resources("AWS::Lambda::EventSourceMapping").values()
    batch = mapping["Properties"]["BatchSize"]
    assert batch == DELIVERER_BATCH_SIZE == 1
    functions = token_keeper.find_resources(
        "AWS::Lambda::Function",
        {"Properties": {"Handler": "token_keeper.handlers.deliverer.handler"}},
    )
    (deliverer_fn,) = functions.values()
    assert deliverer_fn["Properties"]["Timeout"] >= batch * 10


def test_stacks_share_urls_through_ssm_not_exports(
    receiver: Template, token_keeper: Template
) -> None:
    # ADR 0014: no CloudFormation export or import between the stacks.
    for template in (receiver, token_keeper):
        rendered = json.dumps(template.to_json())
        assert "Fn::ImportValue" not in rendered
        assert "Export" not in json.dumps(template.to_json().get("Outputs", {}))
    receiver.has_resource_properties(
        "AWS::SSM::Parameter", {"Name": RECEIVER_URL_PARAM, "Type": "String"}
    )
    token_keeper.has_resource_properties(
        "AWS::SSM::Parameter", {"Name": CONNECT_URL_PARAM, "Type": "String"}
    )
