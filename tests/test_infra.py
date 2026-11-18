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
from receiver.store import KEY_SCHEMA, TTL_ATTRIBUTE

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
            "Properties": Match.object_like(
                {
                    "KeySchema": KEY_SCHEMA,
                    "TimeToLiveSpecification": {
                        "AttributeName": TTL_ATTRIBUTE,
                        "Enabled": True,
                    },
                }
            ),
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


def _statements(template: Template) -> list[dict[str, Any]]:
    policies = template.find_resources("AWS::IAM::Policy")
    return [s for p in policies.values() for s in p["Properties"]["PolicyDocument"]["Statement"]]


def test_lambda_permissions_are_scoped(receiver: Template) -> None:
    statements = _statements(receiver)
    actions = {
        a
        for s in statements
        for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])
    }
    assert {a.split(":")[0] for a in actions} == {"dynamodb", "ssm"}
    assert not any(a.endswith("*") for a in actions), "no wildcard actions"
    for statement in statements:
        resources = statement["Resource"]
        resources = resources if isinstance(resources, list) else [resources]
        assert "*" not in resources, statement
    ssm_resources = str([s["Resource"] for s in statements if "ssm:GetParameter" in s["Action"]])
    for name in SECRET_PARAMS.values():
        assert name.lstrip("/") in ssm_resources
