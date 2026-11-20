"""Prove the DynamoDB IAM conditions on the deployed roles (ADR 0017, docs/deploy.md).

    uv run python scripts/prove_iam.py --profile token-keeper            # simulate
    uv run python scripts/prove_iam.py --profile token-keeper --live     # real calls

Both modes read each deployed Token Keeper Lambda role's own inline policies and check
one table of cases: what each handler really sends must be allowed, and off-pattern
calls (the planner touching a token or the lease, the worker writing a ``STATE#`` key,
and so on) must be denied.

- ``simulate`` asks IAM's policy simulator, with the condition keys given explicitly.
  It's read-only, but it can't show what DynamoDB itself sends. A ``ForAllValues``
  condition is true when the key is absent, so if DynamoDB didn't send
  ``dynamodb:Attributes`` the simulator would still say "denied" while the real
  condition denied nothing.
- ``--live`` settles that. It creates one temporary role per Lambda role, carrying a
  copy of that role's inline policies, and assumes it. Then it sends real DynamoDB
  requests against ``iam-probe`` keys that don't exist, each with a condition that
  can't hold, so nothing is ever written. IAM is checked before the condition:
  ``ConditionalCheckFailed`` (or a plain success for a read) means allowed, and
  ``AccessDenied`` means denied. The temporary roles are deleted at the end, even on
  failure.

Exits non-zero if any case disagrees. The account ID is masked in all output.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
STACK = "TokenKeeper"
HANDLERS = ("connect", "callback", "planner", "worker", "deliverer")
PROBE = "iam-probe"
PLANNER_ATTRS = ["PK", "SK", "next_due_at", "version", "state"]


@dataclass(frozen=True)
class Case:
    handler: str
    action: str  # PutItem | UpdateItem | Query
    pk: str
    allow: bool
    attrs: tuple[str, ...] = ()  # non-key attributes the request names
    return_values: str | None = None
    index: bool = False
    push_due: bool = False  # the planner's exact request shape
    note: str = field(default="", compare=False)


CASES = [
    # connect: the OAuth state record only.
    Case("connect", "PutItem", f"STATE#{PROBE}", True, ("pkce_verifier_ct",)),
    Case("connect", "PutItem", f"ACCOUNT#{PROBE}", False, note="connect writing an account"),
    # deliverer: reads one account and its snapshot.
    Case("deliverer", "Query", f"ACCOUNT#{PROBE}", True),
    Case("deliverer", "Query", f"LINK#{PROBE}", False, note="deliverer reading a user link"),
    # worker: account and snapshot rows only.
    Case("worker", "UpdateItem", f"ACCOUNT#{PROBE}", True, ("lease_owner", "lease_until")),
    Case("worker", "PutItem", f"ACCOUNT#{PROBE}", True, ("snapshot",)),
    Case("worker", "PutItem", f"STATE#{PROBE}", False, note="worker writing a state record"),
    Case("worker", "UpdateItem", f"GHUSER#{PROBE}", False, ("account_id",), note="worker"),
    # planner: next_due_at and version on account rows, nothing returned.
    Case("planner", "UpdateItem", f"ACCOUNT#{PROBE}", True, push_due=True),
    Case("planner", "Query", "active", True, index=True),
    Case("planner", "UpdateItem", f"ACCOUNT#{PROBE}", False, ("access_token_ct",), note="token"),
    Case("planner", "UpdateItem", f"ACCOUNT#{PROBE}", False, ("lease_owner",), note="lease"),
    Case("planner", "UpdateItem", f"ACCOUNT#{PROBE}", False, ("next_due_at",), "ALL_NEW"),
    Case("planner", "UpdateItem", f"LINK#{PROBE}", False, ("next_due_at",), note="other row"),
    # callback: no condition (it writes every prefix); a sanity check that it still works.
    Case("callback", "PutItem", f"LINK#{PROBE}", True, ("account_id",)),
    Case("callback", "UpdateItem", f"ACCOUNT#{PROBE}", True, ("version",)),
]


class Masker:
    def __init__(self, account: str) -> None:
        self.account = account

    def __call__(self, text: str) -> str:
        return text.replace(self.account, "<account>")


def deployed_roles(session: boto3.Session) -> tuple[dict[str, str], str]:
    """Handler name → role ARN, and the table ARN, from the deployed stack."""
    cfn, lam = session.client("cloudformation"), session.client("lambda")
    roles: dict[str, str] = {}
    table_name = None
    paginator = cfn.get_paginator("list_stack_resources")
    for page in paginator.paginate(StackName=STACK):
        for res in page["StackResourceSummaries"]:
            if res["ResourceType"] == "AWS::DynamoDB::Table":
                table_name = res["PhysicalResourceId"]
            if res["ResourceType"] != "AWS::Lambda::Function":
                continue
            config = lam.get_function_configuration(FunctionName=res["PhysicalResourceId"])
            module = config["Handler"].split(".")[-2]
            if module in HANDLERS:
                roles[module] = config["Role"]
    if table_name is None or set(roles) != set(HANDLERS):
        sys.exit(f"stack {STACK} incomplete: found {sorted(roles)}")
    table_arn = session.client("dynamodb").describe_table(TableName=table_name)["Table"]["TableArn"]
    return roles, table_arn


def inline_policies(iam: Any, role_arn: str) -> dict[str, dict[str, Any]]:
    name = role_arn.rsplit("/", 1)[1]
    return {
        policy: iam.get_role_policy(RoleName=name, PolicyName=policy)["PolicyDocument"]
        for policy in iam.list_role_policies(RoleName=name)["PolicyNames"]
    }


# Simulate ---------------------------------------------------------------------


def simulate(iam: Any, role_arn: str, table_arn: str, case: Case) -> bool:
    attrs = ["PK", "SK", *case.attrs] if case.action != "Query" else []
    if case.push_due:
        attrs = PLANNER_ATTRS
    context = [
        {
            "ContextKeyName": "dynamodb:LeadingKeys",
            "ContextKeyValues": [case.pk],
            "ContextKeyType": "stringList",
        }
    ]
    if attrs:
        context.append(
            {
                "ContextKeyName": "dynamodb:Attributes",
                "ContextKeyValues": attrs,
                "ContextKeyType": "stringList",
            }
        )
    if case.return_values:
        context.append(
            {
                "ContextKeyName": "dynamodb:ReturnValues",
                "ContextKeyValues": [case.return_values],
                "ContextKeyType": "string",
            }
        )
    resource = f"{table_arn}/index/due" if case.index else table_arn
    result = iam.simulate_principal_policy(
        PolicySourceArn=role_arn,
        ActionNames=[f"dynamodb:{case.action}"],
        ResourceArns=[resource],
        ContextEntries=context,
    )
    (evaluation,) = result["EvaluationResults"]
    return bool(evaluation["EvalDecision"] == "allowed")


# Live -------------------------------------------------------------------------


def live_call(ddb: Any, table: str, case: Case) -> bool:
    """Send the real request; True if IAM allowed it. Never writes anything."""
    key = {"PK": {"S": case.pk}, "SK": {"S": "META"}}
    never = "attribute_exists(PK)"  # the probe keys don't exist
    try:
        if case.action == "Query" and case.index:
            ddb.query(
                TableName=table,
                IndexName="due",
                KeyConditionExpression="#s = :active AND next_due_at <= :now",
                ExpressionAttributeNames={"#s": "state"},
                ExpressionAttributeValues={":active": {"S": case.pk}, ":now": {"N": "0"}},
            )
        elif case.action == "Query":
            ddb.query(
                TableName=table,
                KeyConditionExpression="PK = :pk",
                ExpressionAttributeValues={":pk": {"S": case.pk}},
            )
        elif case.action == "PutItem":
            item = {**key, **{a: {"S": "x"} for a in case.attrs}}
            ddb.put_item(TableName=table, Item=item, ConditionExpression=never)
        elif case.push_due:
            # store.push_due's exact shape; its condition fails on a missing item.
            ddb.update_item(
                TableName=table,
                Key=key,
                UpdateExpression="SET next_due_at = :until, version = version + :one",
                ConditionExpression="#state = :active AND next_due_at <= :now",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":until": {"N": "2"},
                    ":one": {"N": "1"},
                    ":active": {"S": "active"},
                    ":now": {"N": "1"},
                },
            )
        else:
            names = {f"#a{i}": a for i, a in enumerate(case.attrs)}
            kwargs: dict[str, Any] = {
                "TableName": table,
                "Key": key,
                "UpdateExpression": "SET " + ", ".join(f"{n} = :v" for n in names),
                "ConditionExpression": never,
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": {":v": {"S": "x"}},
            }
            if case.return_values:
                kwargs["ReturnValues"] = case.return_values
            ddb.update_item(**kwargs)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "ConditionalCheckFailedException":
            return True
        if code == "AccessDeniedException":
            return False
        raise
    return True


class ProbeRoles:
    """One temporary role per Lambda role, each with a copy of its inline policies."""

    def __init__(self, session: boto3.Session, account: str) -> None:
        self.iam = session.client("iam")
        self.sts = session.client("sts")
        self.account = account
        self.tag = uuid.uuid4().hex[:8]
        self.created: dict[str, list[str]] = {}

    def create(self, label: str, policies: dict[str, dict[str, Any]]) -> str:
        name = f"token-keeper-iam-probe-{label}-{self.tag}"
        trust = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": f"arn:aws:iam::{self.account}:root"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
        self.iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="token-keeper: temporary IAM proof (scripts/prove_iam.py)",
            MaxSessionDuration=3600,
            Tags=[{"Key": "project", "Value": "token-keeper"}],
        )
        self.created[name] = []
        for policy_name, document in policies.items():
            self.iam.put_role_policy(
                RoleName=name, PolicyName=policy_name, PolicyDocument=json.dumps(document)
            )
            self.created[name].append(policy_name)
        return name

    def client(self, role_name: str, ready: Callable[[Any], bool]) -> Any:
        """A DynamoDB client as the role, once IAM has propagated (``ready`` passes)."""
        arn = f"arn:aws:iam::{self.account}:role/{role_name}"
        deadline = time.time() + 90
        while True:
            try:
                creds = self.sts.assume_role(RoleArn=arn, RoleSessionName="iam-probe")[
                    "Credentials"
                ]
                ddb = boto3.client(
                    "dynamodb",
                    region_name=REGION,
                    aws_access_key_id=creds["AccessKeyId"],
                    aws_secret_access_key=creds["SecretAccessKey"],
                    aws_session_token=creds["SessionToken"],
                )
                if ready(ddb):
                    return ddb
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "AccessDenied":
                    raise
            if time.time() > deadline:
                sys.exit(f"{role_name}: not usable after 90 s")
            time.sleep(5)

    def delete_all(self) -> None:
        for name, policies in self.created.items():
            for policy in policies:
                self.iam.delete_role_policy(RoleName=name, PolicyName=policy)
            self.iam.delete_role(RoleName=name)
        self.created.clear()


def ready_for(table: str, case: Case) -> Callable[[Any], bool]:
    def ready(ddb: Any) -> bool:
        return live_call(ddb, table, case)

    return ready


def without_state(policies: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The planner's policies with ``state`` dropped from its attribute list."""
    copy: dict[str, dict[str, Any]] = json.loads(json.dumps(policies))
    for document in copy.values():
        for statement in document["Statement"]:
            cond = statement.get("Condition", {}).get("ForAllValues:StringEquals", {})
            if "dynamodb:Attributes" in cond:
                kept = [a for a in cond["dynamodb:Attributes"] if a != "state"]
                cond["dynamodb:Attributes"] = kept
    return copy


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    session = boto3.Session(profile_name=args.profile, region_name=REGION)
    account = session.client("sts").get_caller_identity()["Account"]
    mask = Masker(account)
    iam = session.client("iam")
    roles, table_arn = deployed_roles(session)
    table = table_arn.rsplit("/", 1)[1]

    failures = 0

    def report(case: Case, allowed: bool) -> None:
        nonlocal failures
        ok = allowed == case.allow
        failures += not ok
        verdict = "allowed" if allowed else "denied"
        label = f"{case.handler:<9} {case.action:<10} {case.pk:<20} {','.join(case.attrs) or '-'}"
        extra = f" ReturnValues={case.return_values}" if case.return_values else ""
        print(mask(f"{'ok ' if ok else 'BAD'} {label}{extra}: {verdict}"))

    if not args.live:
        print("mode: simulate (IAM policy simulator, deployed role policies)")
        for case in CASES:
            report(case, simulate(iam, roles[case.handler], table_arn, case))
    else:
        print("mode: live (temporary roles with copies of the deployed policies)")
        probes = ProbeRoles(session, account)
        try:
            clients = {}
            for handler in HANDLERS:
                policies = inline_policies(iam, roles[handler])
                name = probes.create(handler, policies)
                first = next(c for c in CASES if c.handler == handler and c.allow)
                clients[handler] = probes.client(name, ready_for(table, first))
            for case in CASES:
                report(case, live_call(clients[case.handler], table, case))
            # Does a name only in the ConditionExpression count towards dynamodb:Attributes?
            planner_policies = without_state(inline_policies(iam, roles["planner"]))
            name = probes.create("planner-nostate", planner_policies)
            push_due = next(c for c in CASES if c.push_due)
            # Ready once its (unchanged) index query works, so a denial below is the
            # condition, not IAM propagation.
            index_query = next(c for c in CASES if c.index)
            ddb = probes.client(name, ready_for(table, index_query))
            counts = not live_call(ddb, table, push_due)
            print(
                "info: with `state` removed from the planner's list, push_due is "
                + ("denied: condition-only names count" if counts else "allowed: they don't")
            )
        finally:
            probes.delete_all()
            print("temporary roles deleted")

    print("PASS" if failures == 0 else f"FAIL ({failures} case(s) disagree)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
