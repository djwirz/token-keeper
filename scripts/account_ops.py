"""Operator steps on one account (docs/runbook.md). Run by Dan, never by CI.

    uv run python scripts/account_ops.py --profile token-keeper create-synthetic --send-sync
    uv run python scripts/account_ops.py --profile token-keeper delete ACCOUNT_ID --receiver

- ``create-synthetic`` makes the RefreshFailed drill's account (spec §8, ADR 0019): a
  real Account row, encrypted like any other, whose refresh token is fake. Its access
  token is already expired, so the next sync refreshes, GitHub answers
  ``bad_refresh_token``, and the worker counts ``RefreshFailed`` and moves it to
  ``needs_reauth``. It belongs to ``u_alarm_drill`` and GitHub user 0, which no real
  user has. ``--send-sync`` puts its sync message on the queue now, rather than
  waiting up to 15 minutes for the planner. The receiver will show its card until
  ``delete --receiver`` removes it.
- ``delete`` is the runbook's "delete an account's data on request": the Account, its
  Snapshot and its links, in one transaction (``Store.delete_account``).
  ``--receiver`` also deletes the demo receiver's row for it: the main app keeps its
  own copy, and on a real request it would delete that itself.

It prints account ids (``acc_…``, not secret) and item keys. The AWS account ID is
masked, error messages included. No token is ever read or printed: the fake ones are
only encrypted.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

import boto3

from token_keeper import crypto, ids, sync
from token_keeper.crypto import Crypto
from token_keeper.store import EncryptedTokens, Identity, Store

REGION = "us-east-1"
APP_ID = "demo"
DRILL_USER = "u_alarm_drill"
# GitHub user ids start at 1: no real user can own this link.
DRILL_GITHUB_USER_ID = 0
DRILL_LOGIN = "alarm-drill"
# Split so secret scanners don't flag fakes. GitHub rejects them as bad_refresh_token.
FAKE_ACCESS = "ghu_" + "AlarmDrillNotARealToken" + "0" * 13
FAKE_REFRESH = "ghr_" + "AlarmDrillNotARealToken" + "0" * 53
REFRESH_LIFETIME = 180 * 24 * 3600


class Masker:
    def __init__(self, account: str) -> None:
        self.account = account

    def __call__(self, text: str) -> str:
        return text.replace(self.account, "<account>")


def stack_resources(session: boto3.Session, stack: str) -> dict[str, tuple[str, str]]:
    """Logical id → (type, physical id)."""
    cfn = session.client("cloudformation")
    found: dict[str, tuple[str, str]] = {}
    for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack):
        for res in page["StackResourceSummaries"]:
            found[res["LogicalResourceId"]] = (res["ResourceType"], res["PhysicalResourceId"])
    return found


def physical(resources: dict[str, tuple[str, str]], kind: str, prefix: str) -> str:
    (match,) = [
        p for logical, (t, p) in resources.items() if t == kind and logical.startswith(prefix)
    ]
    return match


def create_synthetic(store: Store, kms: Crypto, now: int) -> str:
    """The drill account: active, due now, with an expired access token."""
    account_id = ids.new("acc")
    context = crypto.account_context(account_id)
    store.create_account(
        account_id=account_id,
        app_id=APP_ID,
        external_user_id=DRILL_USER,
        identity=Identity(github_user_id=DRILL_GITHUB_USER_ID, login=DRILL_LOGIN),
        tokens=EncryptedTokens(
            access_token_ct=kms.encrypt(FAKE_ACCESS, context),
            refresh_token_ct=kms.encrypt(FAKE_REFRESH, context),
            access_expires_at=now,
            refresh_expires_at=now + REFRESH_LIFETIME,
        ),
        snapshot={
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "profile": {
                "name": "Alarm drill",
                "avatar_url": None,
                "public_repos": 0,
                "followers": 0,
            },
            "recent_events": [],
        },
        next_due_at=now,
    )
    return account_id


def delete_receiver_row(ddb: Any, table: str, account_id: str) -> bool:
    response = ddb.delete_item(
        TableName=table, Key={"PK": {"S": f"ACCOUNT#{account_id}"}}, ReturnValues="ALL_OLD"
    )
    return "Attributes" in response


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--profile", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-synthetic", help="the RefreshFailed drill's account")
    create.add_argument("--send-sync", action="store_true", help="enqueue its sync now")
    delete = commands.add_parser("delete", help="delete one account's data")
    delete.add_argument("account_id")
    delete.add_argument("--receiver", action="store_true", help="also the receiver's row")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=REGION)
    mask = Masker(session.client("sts").get_caller_identity()["Account"])
    try:
        run(session, args, mask)
    except Exception as exc:
        # AWS error messages carry ARNs, so they go through the masker too.
        sys.exit(mask(f"error: {exc}"))


def run(session: boto3.Session, args: argparse.Namespace, mask: Masker) -> None:
    resources = stack_resources(session, "TokenKeeper")
    table = physical(resources, "AWS::DynamoDB::Table", "Table")
    store = Store(table, client=session.client("dynamodb"))

    if args.command == "create-synthetic":
        if store.account_for_user(APP_ID, DRILL_USER):
            sys.exit(f"{DRILL_USER} already has an account: delete it first")
        key_arn = physical(resources, "AWS::KMS::Key", "TokenKey")
        kms = Crypto(key_arn, client=session.client("kms"))
        account_id = create_synthetic(store, kms, int(time.time()))
        print(f"created {account_id} ({DRILL_USER}, needs a refresh, due now)")
        if args.send_sync:
            queue = physical(resources, "AWS::SQS::Queue", "SyncQueue")
            sync.enqueue(
                queue, [account_id], correlation_id=ids.new("cor"), client=session.client("sqs")
            )
            print("sync message sent")
        return

    deleted = store.delete_account(args.account_id)
    if not deleted:
        print(f"no account {args.account_id}")
    for key in deleted:
        print(mask(f"deleted {key}"))
    if args.receiver:
        receiver = stack_resources(session, "DemoReceiver")
        receiver_table = physical(receiver, "AWS::DynamoDB::Table", "Table")
        found = delete_receiver_row(session.client("dynamodb"), receiver_table, args.account_id)
        print("deleted the receiver's row" if found else "the receiver had no row")


if __name__ == "__main__":
    main()
