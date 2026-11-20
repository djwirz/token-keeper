"""CDK assertion tests: the infrastructure says what the spec says (spec §8-§10)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk import aws_lambda as lambda_
from aws_cdk.assertions import Match, Template

from infra.app import ENV
from infra.stacks.alerts import AlertsStack
from infra.stacks.budget import BudgetStack
from infra.stacks.ci_roles import (
    DEPLOY_SUBJECT,
    DIFF_SUBJECT,
    CiRolesStack,
)
from infra.stacks.demo_receiver import SECRET_PARAMS, DemoReceiverStack
from infra.stacks.shared import (
    ALARM_TOPIC_NAME,
    CONNECT_URL_PARAM,
    GITHUB_CLIENT_ID_PARAM,
    RECEIVER_URL_PARAM,
)
from infra.stacks.token_keeper import (
    DELIVERER_BATCH_SIZE,
    PLANNER_ATTRIBUTES,
    WORKER_BATCH_SIZE,
    WORKER_TIMEOUT_SECONDS,
    TokenKeeperStack,
)
from infra.stacks.token_keeper import SECRET_PARAMS as TK_SECRET_PARAMS
from receiver.store import KEY_SCHEMA
from token_keeper import metrics
from token_keeper.handlers.deliverer import MAX_RECEIVES
from token_keeper.handlers.worker import MAX_RECEIVES as SYNC_MAX_RECEIVES
from token_keeper.log import SERVICE
from token_keeper.store import ATTRIBUTE_DEFINITIONS as TK_ATTRIBUTE_DEFINITIONS
from token_keeper.store import GLOBAL_SECONDARY_INDEXES, LEASE_SECONDS, TTL_ATTRIBUTE
from token_keeper.store import KEY_SCHEMA as TK_KEY_SCHEMA

# The app's own environment: region only, so templates hold ${AWS::AccountId} rather than
# a literal account (ADR 0016).
ACCOUNT = "${AWS::AccountId}"


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
                {
                    "BudgetLimit": {"Amount": 5, "Unit": "USD"},
                    "TimeUnit": "MONTHLY",
                    # Before credits, or a credit-funded account never alerts.
                    "CostTypes": {"IncludeCredit": False},
                }
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
        (action, f"arn:${{AWS::Partition}}:ssm:{ENV.region}:{ACCOUNT}:parameter{name}")
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
            "AttributeDefinitions": Match.array_with(TK_ATTRIBUTE_DEFINITIONS),
            # The due index, keys only (no ciphertext copied), as moto and the runner build it.
            "GlobalSecondaryIndexes": GLOBAL_SECONDARY_INDEXES,
            "TimeToLiveSpecification": {"AttributeName": TTL_ATTRIBUTE, "Enabled": True},
            "BillingMode": "PAY_PER_REQUEST",
        },
    )


def test_only_the_callback_and_worker_can_decrypt(token_keeper: Template) -> None:
    grants = _grants_by_role(token_keeper)
    decrypters = {
        role for role, pairs in grants.items() if any(a == "kms:Decrypt" for a, _ in pairs)
    }
    assert decrypters == {
        _role_of(token_keeper, "token_keeper.handlers.callback.handler"),
        _role_of(token_keeper, "token_keeper.handlers.worker.handler"),
    }
    # And nothing anywhere grants kms:* or a wildcard action.
    for pairs in grants.values():
        assert not any(action.endswith("*") for action, _ in pairs)


def _logical_id(template: Template, resource_type: str, prefix: str) -> str:
    (logical,) = [k for k in template.find_resources(resource_type) if k.startswith(prefix)]
    return logical


def test_each_role_has_exactly_what_its_handler_needs(token_keeper: Template) -> None:
    (table_id,) = token_keeper.find_resources("AWS::DynamoDB::Table")
    (key_id,) = token_keeper.find_resources("AWS::KMS::Key")
    queue_id = _logical_id(token_keeper, "AWS::SQS::Queue", "DeliveryQueue")
    sync_id = _logical_id(token_keeper, "AWS::SQS::Queue", "SyncQueue")
    planner_id = _logical_id(token_keeper, "AWS::Lambda::Function", "Planner")
    table, key, queue = f"${{{table_id}.Arn}}", f"${{{key_id}.Arn}}", f"${{{queue_id}.Arn}}"
    sync_queue = f"${{{sync_id}.Arn}}"
    send = {"sqs:SendMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl"}
    consume = {
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:ChangeMessageVisibility",
        "sqs:GetQueueAttributes",
        "sqs:GetQueueUrl",
    }

    def ssm(name: str) -> set[tuple[str, str]]:
        arn = f"arn:${{AWS::Partition}}:ssm:{ENV.region}:{ACCOUNT}:parameter{name}"
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
        }
        | {(a, queue) for a in send}
        # The immediate sync after connect (spec §4).
        | {(a, sync_queue) for a in send}
        | ssm(TK_SECRET_PARAMS["GITHUB_CLIENT_SECRET_PARAM"])
        | ssm(GITHUB_CLIENT_ID_PARAM),
        "token_keeper.handlers.deliverer.handler": {("dynamodb:Query", table)}
        | {(a, queue) for a in consume}
        | ssm(TK_SECRET_PARAMS["WEBHOOK_SECRET_PARAM"])
        | ssm(RECEIVER_URL_PARAM),
        # Spec §9: query the due index (and nothing else), push next_due_at, enqueue.
        "token_keeper.handlers.planner.handler": {
            ("dynamodb:Query", f"{table}/index/due"),
            ("dynamodb:UpdateItem", table),
        }
        | {(a, sync_queue) for a in send},
        "token_keeper.handlers.worker.handler": {
            ("kms:Encrypt", key),
            ("kms:Decrypt", key),
            ("dynamodb:UpdateItem", table),
            ("dynamodb:PutItem", table),
        }
        | {(a, sync_queue) for a in consume}
        | {(a, queue) for a in send}
        | ssm(TK_SECRET_PARAMS["GITHUB_CLIENT_SECRET_PARAM"])
        | ssm(GITHUB_CLIENT_ID_PARAM),
        # Spec §6.4, ADR 0018: read a GitHub link, take over the account, enqueue. No KMS.
        "token_keeper.handlers.github_webhook.handler": {
            ("dynamodb:GetItem", table),
            ("dynamodb:UpdateItem", table),
        }
        | {(a, queue) for a in send}
        | ssm(TK_SECRET_PARAMS["GITHUB_WEBHOOK_SECRET_PARAM"]),
    }
    grants = _grants_by_role(token_keeper)
    scheduler_role = _logical_id(token_keeper, "AWS::IAM::Role", "SchedulerRoleForTarget")
    planner = f"${{{planner_id}.Arn}}"
    assert set(grants) == {_role_of(token_keeper, h) for h in expected} | {scheduler_role}
    # Active tracing (ADR 0019) adds these two to every function's role, and nothing else.
    xray = {("xray:PutTraceSegments", "*"), ("xray:PutTelemetryRecords", "*")}
    for handler_path, pairs in expected.items():
        assert grants[_role_of(token_keeper, handler_path)] == pairs | xray, handler_path
    # The schedule's role can invoke the planner and nothing else.
    assert grants[scheduler_role] == {
        ("lambda:InvokeFunction", planner),
        ("lambda:InvokeFunction", f"{planner}:*"),
    }


def test_both_queues_have_a_dlq_and_partial_batch_failures(token_keeper: Template) -> None:
    queues = token_keeper.find_resources("AWS::SQS::Queue")
    assert len(queues) == 4
    for prefix, receives in (("DeliveryQueue", MAX_RECEIVES), ("SyncQueue", SYNC_MAX_RECEIVES)):
        queue = queues[_logical_id(token_keeper, "AWS::SQS::Queue", prefix)]
        dlq = prefix.replace("Queue", "Dlq")
        policy = queue["Properties"]["RedrivePolicy"]
        assert policy["maxReceiveCount"] == receives
        assert policy["deadLetterTargetArn"]["Fn::GetAtt"][0].startswith(dlq)
    mappings = token_keeper.find_resources("AWS::Lambda::EventSourceMapping").values()
    assert len(mappings) == 2
    for mapping in mappings:
        assert mapping["Properties"]["FunctionResponseTypes"] == ["ReportBatchItemFailures"]


def _function(template: Template, module: str) -> dict[str, Any]:
    functions = template.find_resources(
        "AWS::Lambda::Function",
        {"Properties": {"Handler": f"token_keeper.handlers.{module}.handler"}},
    )
    (function,) = functions.values()
    return dict(function)


def test_worker_cannot_outlive_its_lease(token_keeper: Template) -> None:
    # ADR 0004/0015: a worker that outlived its lease could refresh with a pair a second
    # worker had already rotated.
    worker = _function(token_keeper, "worker")
    assert worker["Properties"]["Timeout"] == WORKER_TIMEOUT_SECONDS < LEASE_SECONDS
    sync_id = _logical_id(token_keeper, "AWS::SQS::Queue", "SyncQueue")
    (mapping,) = token_keeper.find_resources(
        "AWS::Lambda::EventSourceMapping",
        {"Properties": {"EventSourceArn": {"Fn::GetAtt": [sync_id, "Arn"]}}},
    ).values()
    assert mapping["Properties"]["BatchSize"] == WORKER_BATCH_SIZE == 1
    queue = token_keeper.find_resources("AWS::SQS::Queue")[sync_id]
    assert queue["Properties"]["VisibilityTimeout"] >= 6 * WORKER_TIMEOUT_SECONDS


def test_planner_runs_every_15_minutes(token_keeper: Template) -> None:
    planner_id = _logical_id(token_keeper, "AWS::Lambda::Function", "Planner")
    token_keeper.has_resource_properties(
        "AWS::Scheduler::Schedule",
        {
            "ScheduleExpression": "rate(15 minutes)",
            "State": "ENABLED",
            "Target": Match.object_like({"Arn": {"Fn::GetAtt": [planner_id, "Arn"]}}),
        },
    )


def test_token_keeper_secrets_are_ssm_names_and_logs_kept_two_weeks(
    token_keeper: Template,
) -> None:
    functions = token_keeper.find_resources("AWS::Lambda::Function")
    assert len(functions) == 6
    for function in functions.values():
        variables = function["Properties"]["Environment"]["Variables"]
        for param_env in TK_SECRET_PARAMS:
            assert param_env.removesuffix("_PARAM") not in variables
        assert function["Properties"]["Architectures"] == ["arm64"]
    log_groups = token_keeper.find_resources("AWS::Logs::LogGroup")
    assert len(log_groups) == 6
    assert all(g["Properties"]["RetentionInDays"] == 14 for g in log_groups.values())


def test_token_keeper_api_routes_and_throttle(token_keeper: Template) -> None:
    routes = token_keeper.find_resources("AWS::ApiGatewayV2::Route")
    assert {r["Properties"]["RouteKey"] for r in routes.values()} == {
        "GET /connect",
        "GET /callback",
        "POST /github/webhook",
    }
    token_keeper.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        {
            "DefaultRouteSettings": Match.object_like(
                {"ThrottlingRateLimit": 5, "ThrottlingBurstLimit": 10}
            )
        },
    )


def test_github_webhook_answers_inside_githubs_ten_seconds(token_keeper: Template) -> None:
    # GitHub records a delivery as failed after 10 s (its "handling failed deliveries" docs).
    assert _function(token_keeper, "github_webhook")["Properties"]["Timeout"] < 10
    outputs = token_keeper.to_json()["Outputs"]
    url = json.dumps(outputs["GitHubWebhookUrl"]["Value"])
    assert url.endswith('/github/webhook"]]}')


def test_deliverer_timeout_covers_its_whole_batch(token_keeper: Template) -> None:
    # A batch that outlives the Lambda is killed before any backoff is set, and every
    # message burns a receive per visibility timeout (PR 3 review). Allow 10 s per
    # message: one POST is at most 2 s to connect plus 5 s to read.
    queue_id = _logical_id(token_keeper, "AWS::SQS::Queue", "DeliveryQueue")
    (mapping,) = token_keeper.find_resources(
        "AWS::Lambda::EventSourceMapping",
        {"Properties": {"EventSourceArn": {"Fn::GetAtt": [queue_id, "Arn"]}}},
    ).values()
    batch = mapping["Properties"]["BatchSize"]
    assert batch == DELIVERER_BATCH_SIZE == 1
    assert _function(token_keeper, "deliverer")["Properties"]["Timeout"] >= batch * 10


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


# IAM conditions (ADR 0017) ------------------------------------------------------


def _leading_keys(prefix: str) -> dict[str, Any]:
    return {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [f"{prefix}*"]}}


def _table_statements(template: Template) -> dict[str, list[tuple[frozenset[str], str, Any]]]:
    """Each role's DynamoDB statements as (actions, resource, condition)."""
    by_role: dict[str, list[tuple[frozenset[str], str, Any]]] = {}
    for policy in template.find_resources("AWS::IAM::Policy").values():
        (role,) = policy["Properties"]["Roles"]
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = [actions] if isinstance(actions, str) else actions
            if not all(a.startswith("dynamodb:") for a in actions):
                continue
            (resource,) = (
                statement["Resource"]
                if isinstance(statement["Resource"], list)
                else [statement["Resource"]]
            )
            by_role.setdefault(role["Ref"], []).append(
                (frozenset(actions), _flatten(resource), statement.get("Condition"))
            )
    return by_role


def test_table_grants_carry_exactly_their_key_and_attribute_conditions(
    token_keeper: Template,
) -> None:
    # An exact match per role: dropping or loosening a condition fails here.
    (table_id,) = token_keeper.find_resources("AWS::DynamoDB::Table")
    table = f"${{{table_id}.Arn}}"
    expected: dict[str, list[tuple[frozenset[str], str, Any]]] = {
        "connect": [(frozenset({"dynamodb:PutItem"}), table, _leading_keys("STATE#"))],
        # Writes all four key prefixes, so a LeadingKeys condition would exclude nothing.
        "callback": [
            (
                frozenset(
                    {
                        "dynamodb:GetItem",
                        "dynamodb:DeleteItem",
                        "dynamodb:PutItem",
                        "dynamodb:UpdateItem",
                    }
                ),
                table,
                None,
            )
        ],
        "deliverer": [(frozenset({"dynamodb:Query"}), table, _leading_keys("ACCOUNT#"))],
        "planner": [
            # On the index the leading key is `state`, so no LeadingKeys here.
            (frozenset({"dynamodb:Query"}), f"{table}/index/due", None),
            (
                frozenset({"dynamodb:UpdateItem"}),
                table,
                {
                    **_leading_keys("ACCOUNT#"),
                    "ForAllValues:StringEquals": {"dynamodb:Attributes": list(PLANNER_ATTRIBUTES)},
                    "StringEqualsIfExists": {"dynamodb:ReturnValues": "NONE"},
                },
            ),
        ],
        "worker": [
            (
                frozenset({"dynamodb:UpdateItem", "dynamodb:PutItem"}),
                table,
                _leading_keys("ACCOUNT#"),
            )
        ],
        # Two statements: the link it reads and the account it takes over (ADR 0018).
        "github_webhook": [
            (frozenset({"dynamodb:GetItem"}), table, _leading_keys("GHUSER#")),
            (frozenset({"dynamodb:UpdateItem"}), table, _leading_keys("ACCOUNT#")),
        ],
    }
    statements = _table_statements(token_keeper)
    for handler, want in expected.items():
        got = statements[_role_of(token_keeper, f"token_keeper.handlers.{handler}.handler")]
        assert sorted(got, key=repr) == sorted(want, key=repr), handler


def test_planner_attribute_list_covers_exactly_what_push_due_names() -> None:
    # The IAM list (dynamodb:Attributes) must match the code: a missing name breaks the
    # planner in AWS (and moto wouldn't notice), and an extra one widens the grant.
    from unittest.mock import MagicMock

    from token_keeper.store import Store

    client = MagicMock()
    Store("t", client=client).push_due("acc_x", now=100, until=200)
    ((), call) = client.update_item.call_args
    names = set(call["Key"]) | set(call.get("ExpressionAttributeNames", {}).values())
    for expression in (call["UpdateExpression"], call.get("ConditionExpression", "")):
        names |= set(re.findall(r"(?<![:#\w])([A-Za-z_]\w*)", expression))
    names -= {"SET", "REMOVE", "AND", "OR", "NOT", "attribute_exists", "attribute_not_exists"}
    assert names == set(PLANNER_ATTRIBUTES)
    assert "ReturnValues" not in call or call["ReturnValues"] == "NONE"


# CI roles and account-free templates (ADR 0016) --------------------------------


@pytest.fixture(scope="module")
def ci_roles() -> Template:
    return Template.from_stack(CiRolesStack(cdk.App(), "CiRoles", env=ENV))


def _bootstrap_role(kind: str) -> str:
    return (
        f"arn:${{AWS::Partition}}:iam::{ACCOUNT}:role/cdk-hnb659fds-{kind}-role-"
        f"{ACCOUNT}-{ENV.region}"
    )


def test_ci_roles_trust_one_github_subject_each_and_only_assume_bootstrap_roles(
    ci_roles: Template,
) -> None:
    (provider_id,) = ci_roles.find_resources("AWS::IAM::OIDCProvider")
    ci_roles.has_resource_properties(
        "AWS::IAM::OIDCProvider",
        {
            "Url": "https://token.actions.githubusercontent.com",
            "ClientIdList": ["sts.amazonaws.com"],
        },
    )
    roles = ci_roles.find_resources("AWS::IAM::Role")
    trusted = {}
    for logical, role in roles.items():
        (statement,) = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
        assert statement["Principal"] == {"Federated": {"Fn::GetAtt": [provider_id, "Arn"]}}
        assert statement["Condition"] == {
            "StringEquals": {
                "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                "token.actions.githubusercontent.com:sub": statement["Condition"]["StringEquals"][
                    "token.actions.githubusercontent.com:sub"
                ],
            }
        }
        trusted[logical.split("Role")[0]] = statement["Condition"]["StringEquals"][
            "token.actions.githubusercontent.com:sub"
        ]
        assert role["Properties"]["MaxSessionDuration"] == 3600
    assert trusted == {"Deploy": DEPLOY_SUBJECT, "Diff": DIFF_SUBJECT}
    # GitHub's immutable subjects (owner and repo by name and ID), as the repo's
    # actions/oidc/customization/sub reports them. The name-only form was what slice 4
    # first deployed, and every assume failed: pinning our own constant proved nothing.
    assert DEPLOY_SUBJECT == "repo:djwirz@15788618/token-keeper@1463349634:environment:demo"
    assert DIFF_SUBJECT == "repo:djwirz@15788618/token-keeper@1463349634:pull_request"
    grants = _grants_by_role(ci_roles)
    by_name = {logical.split("Role")[0]: logical for logical in roles}
    assert grants[by_name["Deploy"]] == {
        ("sts:AssumeRole", _bootstrap_role(kind))
        for kind in ("deploy", "file-publishing", "lookup")
    }
    assert grants[by_name["Diff"]] == {("sts:AssumeRole", _bootstrap_role("lookup"))}


def test_budget_and_ci_roles_deploy_without_a_bootstrap() -> None:
    # What the CLI reads is the cloud assembly's manifest, not the template: it must
    # name no role to assume and need no bootstrap stack, or the budget can't go first.
    # (A template-only version of this test passed with BootstraplessSynthesizer, whose
    # first real deploy then failed trying to assume the bootstrap deploy role.)
    app = cdk.App()
    BudgetStack(app, "Budget", email="ci@example.invalid", env=ENV)
    AlertsStack(app, "Alerts", email="ci@example.invalid", env=ENV)
    CiRolesStack(app, "CiRoles", env=ENV)
    assembly = app.synth()
    for name in ("Budget", "Alerts", "CiRoles"):
        stack = assembly.get_stack_by_name(name)
        assert stack.assume_role_arn is None, name
        assert stack.cloud_formation_execution_role_arn is None, name
        assert stack.requires_bootstrap_stack_version is None, name
        assert "BootstrapVersion" not in stack.template.get("Parameters", {}), name
        assert stack.assets == [], name


def test_no_template_holds_an_account_id(
    budget: Template,
    alerts: Template,
    ci_roles: Template,
    receiver: Template,
    token_keeper: Template,
) -> None:
    for template in (budget, alerts, ci_roles, receiver, token_keeper):
        assert not re.search(r"\b\d{12}\b", json.dumps(template.to_json()))


# Observability (slice 6, spec §8, ADR 0019) ----------------------------------------

# The Alerts stack's topic, by name. The test app writes the partition as a Ref; the real
# app (with cdk.json's context) writes "aws". Either is right; an account literal isn't.
TOPIC_ARN = re.compile(
    rf"arn:(aws|\$\{{AWS::Partition\}}):sns:{ENV.region}:\$\{{AWS::AccountId\}}:{ALARM_TOPIC_NAME}"
)


def _is_topic(actions: list[Any]) -> bool:
    return len(actions) == 1 and TOPIC_ARN.fullmatch(_flatten(actions[0])) is not None


@pytest.fixture(scope="module")
def alerts() -> Template:
    return Template.from_stack(
        AlertsStack(cdk.App(), "Alerts", email="ci@example.invalid", env=ENV)
    )


def test_alerts_stack_is_the_named_topic_and_one_email(alerts: Template) -> None:
    alerts.resource_count_is("AWS::SNS::Topic", 1)
    alerts.has_resource_properties("AWS::SNS::Topic", {"TopicName": ALARM_TOPIC_NAME})
    alerts.resource_count_is("AWS::SNS::Subscription", 1)
    alerts.has_resource_properties(
        "AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": "ci@example.invalid"}
    )


def test_ci_never_builds_the_alerts_stack(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # CI has no ALERT_EMAIL, so it can't synth or deploy the stack that holds the
    # address (ADR 0016, ADR 0019). TokenKeeper doesn't depend on it either.
    import infra.app as app_module

    monkeypatch.delenv("ALERT_EMAIL", raising=False)
    monkeypatch.delenv("BUDGET_EMAIL", raising=False)
    build = tmp_path / "lambda"
    (build / "token_keeper" / "handlers").mkdir(parents=True)
    (build / "token_keeper" / "handlers" / "callback.py").write_text("")
    monkeypatch.setattr(app_module, "LAMBDA_BUILD", build)
    apps: list[cdk.App] = []

    class RecordingApp(cdk.App):
        def __init__(self) -> None:
            super().__init__(outdir=str(tmp_path / "out"))
            apps.append(self)

    monkeypatch.setattr(cdk, "App", RecordingApp)
    app_module.main()
    (app,) = apps
    names = {c.node.id for c in app.node.children if isinstance(c, cdk.Stack)}
    assert names == {"CiRoles", "DemoReceiver", "TokenKeeper"}

    # With the variable set (Dan's desk only), it's there.
    monkeypatch.setenv("ALERT_EMAIL", "ci@example.invalid")
    app_module.main()
    assert "Alerts" in {c.node.id for c in apps[1].node.children}


def test_the_token_keeper_stack_creates_no_topic(token_keeper: Template) -> None:
    token_keeper.resource_count_is("AWS::SNS::Topic", 0)
    token_keeper.resource_count_is("AWS::SNS::Subscription", 0)


def test_every_function_is_traced_and_names_the_metrics_namespace(
    token_keeper: Template,
) -> None:
    functions = token_keeper.find_resources("AWS::Lambda::Function")
    assert len(functions) == 6
    for logical, function in functions.items():
        props = function["Properties"]
        assert props["TracingConfig"] == {"Mode": "Active"}, logical
        env = props["Environment"]["Variables"]
        assert env["POWERTOOLS_METRICS_NAMESPACE"] == metrics.NAMESPACE, logical
        assert env["POWERTOOLS_SERVICE_NAME"] == SERVICE, logical


def _alarm(template: Template, name: str) -> dict[str, Any]:
    (alarm,) = template.find_resources(
        "AWS::CloudWatch::Alarm", {"Properties": {"AlarmName": name}}
    ).values()
    props: dict[str, Any] = alarm["Properties"]
    return props


def test_there_are_exactly_three_alarms_and_each_emails_on_alarm_and_ok(
    token_keeper: Template,
) -> None:
    alarms = token_keeper.find_resources("AWS::CloudWatch::Alarm")
    assert len(alarms) == 3
    for logical, alarm in alarms.items():
        props = alarm["Properties"]
        assert _is_topic(props["AlarmActions"]), logical
        assert _is_topic(props["OKActions"]), logical
        assert "docs/runbook.md#" in props["AlarmDescription"], logical


def test_dlq_alarm_watches_both_dlqs(token_keeper: Template) -> None:
    props = _alarm(token_keeper, "token-keeper-dlq-messages")
    assert props["Threshold"] == 1
    assert props["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
    assert props["EvaluationPeriods"] == 1
    assert props["TreatMissingData"] == "notBreaching"
    by_id = {m["Id"]: m for m in props["Metrics"]}
    assert by_id["expr_1"]["Expression"] == "MAX([FILL(sync, 0), FILL(delivery, 0)])"
    sync_dlq = _logical_id(token_keeper, "AWS::SQS::Queue", "SyncDlq")
    delivery_dlq = _logical_id(token_keeper, "AWS::SQS::Queue", "DeliveryDlq")
    watched = set()
    for key in ("sync", "delivery"):
        stat = by_id[key]["MetricStat"]
        assert stat["Metric"]["MetricName"] == "ApproximateNumberOfMessagesVisible"
        assert stat["Stat"] == "Maximum"
        (dimension,) = stat["Metric"]["Dimensions"]
        watched.add(dimension["Value"]["Fn::GetAtt"][0])
    assert watched == {sync_dlq, delivery_dlq}


def test_refresh_failed_alarm_is_one_in_an_hour(token_keeper: Template) -> None:
    props = _alarm(token_keeper, "token-keeper-refresh-failed")
    assert props["Namespace"] == metrics.NAMESPACE
    assert props["MetricName"] == metrics.REFRESH_FAILED
    # The one dimension the handlers emit, or the alarm watches nothing.
    assert props["Dimensions"] == [{"Name": "service", "Value": SERVICE}]
    assert props["Statistic"] == "Sum"
    assert props["Threshold"] == 1
    assert props["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
    assert props["Period"] * props["EvaluationPeriods"] == 3600
    assert props["DatapointsToAlarm"] == 1
    assert props["TreatMissingData"] == "notBreaching"


def test_planner_missing_alarm_breaches_on_45_minutes_of_silence(
    token_keeper: Template,
) -> None:
    props = _alarm(token_keeper, "token-keeper-planner-missing")
    planner_id = _logical_id(token_keeper, "AWS::Lambda::Function", "Planner")
    assert props["Namespace"] == "AWS/Lambda"
    assert props["MetricName"] == "Invocations"
    assert props["Dimensions"] == [{"Name": "FunctionName", "Value": {"Ref": planner_id}}]
    assert props["Statistic"] == "Sum"
    assert props["Threshold"] == 1
    assert props["ComparisonOperator"] == "LessThanThreshold"
    assert props["Period"] * props["EvaluationPeriods"] == 45 * 60
    assert props["DatapointsToAlarm"] == props["EvaluationPeriods"]
    # No data is the failure: a disabled schedule emits nothing at all.
    assert props["TreatMissingData"] == "breaching"


def test_the_dashboard_shows_every_metric_and_each_lambdas_errors(
    token_keeper: Template,
) -> None:
    (dashboard,) = token_keeper.find_resources("AWS::CloudWatch::Dashboard").values()
    body = json.dumps(dashboard["Properties"]["DashboardBody"])
    for name in metrics.ALL:
        assert f'\\"{name}\\"' in body, name
    assert body.count('\\"Errors\\"') == 6
    assert body.count('\\"ApproximateNumberOfMessagesVisible\\"') == 4
