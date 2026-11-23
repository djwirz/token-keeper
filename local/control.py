"""Local-only controls for the runner, on 127.0.0.1 (never part of a Lambda).

- ``POST /plan``: run the planner now, instead of waiting for the next tick.
- ``POST /expire``: mark every active account's access token as expired and make it
  due, then run the planner. The next sync then makes a real refresh against GitHub,
  which is how slice 3's live rotation check works without waiting eight hours.
- ``GET /account``: the demo account's facts (``local/facts.py``) and the sync interval
  the worker would use now, for ``scripts/demo_gif.py --local``. Read-only.
- ``POST /revoke``: the local stand-in for GitHub's ``github_app_authorization``
  webhook, which can't reach localhost without a proxy. It makes the same takeover
  write and enqueues the same event as ``github_webhook.handler``, for the demo user's
  account only.

Only ``/expire`` and ``/revoke`` write, and no token is ever read here.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import boto3

from local.facts import read_facts
from local.http import LambdaHandler, router
from token_keeper import delivery, ids, sync
from token_keeper.handlers import planner
from token_keeper.handlers._shared import store
from token_keeper.handlers.github_webhook import REVOKED


def _json(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def expire_access_tokens(table: str, now: int) -> int:
    dynamodb = boto3.client("dynamodb")
    items = dynamodb.scan(
        TableName=table,
        FilterExpression="SK = :meta AND #state = :active",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={":meta": {"S": "META"}, ":active": {"S": "active"}},
        ProjectionExpression="PK, SK",
    )["Items"]
    for item in items:
        dynamodb.update_item(
            TableName=table,
            Key={"PK": item["PK"], "SK": item["SK"]},
            # Local test harness only: unlike every real writer (spec §4), it doesn't
            # bump version. It's a shortcut for the live rotation check, not a writer
            # the lease rules need to account for.
            UpdateExpression="SET access_expires_at = :now, next_due_at = :now",
            ExpressionAttributeValues={":now": {"N": str(now)}},
        )
    return len(items)


def control_handler() -> LambdaHandler:
    def plan(event: dict[str, Any], context: object) -> dict[str, Any]:
        return _json(200, planner.handler({}, None))

    def expire(event: dict[str, Any], context: object) -> dict[str, Any]:
        expired = expire_access_tokens(os.environ["TOKEN_KEEPER_TABLE"], int(time.time()))
        return _json(200, {"expired": expired, **planner.handler({}, None)})

    def account(event: dict[str, Any], context: object) -> dict[str, Any]:
        facts = read_facts(boto3.client("dynamodb"), os.environ["TOKEN_KEEPER_TABLE"])
        return _json(
            200,
            {
                "account": facts.to_json() if facts else None,
                "sync_interval_minutes": sync.sync_interval_seconds() // 60,
            },
        )

    def revoke(event: dict[str, Any], context: object) -> dict[str, Any]:
        facts = read_facts(boto3.client("dynamodb"), os.environ["TOKEN_KEEPER_TABLE"])
        if facts is None:
            return _json(404, {"error": "no demo account"})
        written = store().mark_needs_reauth_takeover(
            facts.account_id, reason=REVOKED, delivery=None
        )
        delivery.enqueue(
            os.environ["DELIVERY_QUEUE_URL"],
            account_id=facts.account_id,
            event_type="account.updated",
            correlation_id=ids.new("cor"),
        )
        return _json(200, {"revoked": written})

    return router(
        {
            ("POST", "/plan"): plan,
            ("POST", "/expire"): expire,
            ("GET", "/account"): account,
            ("POST", "/revoke"): revoke,
        }
    )
