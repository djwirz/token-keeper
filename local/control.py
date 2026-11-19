"""Local-only controls for the runner, on 127.0.0.1 (never part of a Lambda).

- ``POST /plan``: run the planner now, instead of waiting for the next tick.
- ``POST /expire``: mark every active account's access token as expired and make it
  due, then run the planner. The next sync then makes a real refresh against GitHub,
  which is how slice 3's live rotation check works without waiting eight hours.

Only times change; no token is read or written here.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import boto3

from local.http import LambdaHandler, router
from token_keeper.handlers import planner


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

    return router({("POST", "/plan"): plan, ("POST", "/expire"): expire})
