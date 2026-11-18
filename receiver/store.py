"""The receiver's table: the latest event per account, plus webhook-id dedupe markers.

Items:
- ``PK=ACCOUNT#<account_id>``: ``version`` (N), ``event`` (S, the event as JSON)
- ``PK=MSG#<webhook_id>``: ``expires_at`` (N, TTL)

Both are written in one transaction, so a duplicate delivery or an older version
changes nothing.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, Literal

import boto3
from botocore.exceptions import ClientError

from token_keeper.contract.events import AccountEvent, parse_event

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient

SaveResult = Literal["stored", "duplicate", "stale"]
DEDUPE_TTL_SECONDS = 2 * 24 * 60 * 60  # deliveries are retried for about a day

# Shared with the local runner and checked against CDK by tests/test_infra.py.
KEY_SCHEMA = [{"AttributeName": "PK", "KeyType": "HASH"}]
ATTRIBUTE_DEFINITIONS = [{"AttributeName": "PK", "AttributeType": "S"}]
TTL_ATTRIBUTE = "expires_at"


class Store:
    def __init__(self, table_name: str, client: DynamoDBClient | None = None) -> None:
        self._table = table_name
        self._client: DynamoDBClient = client or boto3.client("dynamodb")

    def save_event(
        self, webhook_id: str, event: AccountEvent, now: int | None = None
    ) -> SaveResult:
        current = int(time.time()) if now is None else now
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table,
                            "Item": {
                                "PK": {"S": f"MSG#{webhook_id}"},
                                TTL_ATTRIBUTE: {"N": str(current + DEDUPE_TTL_SECONDS)},
                            },
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table,
                            "Item": {
                                "PK": {"S": f"ACCOUNT#{event.account_id}"},
                                "version": {"N": str(event.version)},
                                "event": {"S": json.dumps(event.to_dict())},
                            },
                            "ConditionExpression": "attribute_not_exists(PK) OR version < :v",
                            "ExpressionAttributeValues": {":v": {"N": str(event.version)}},
                        }
                    },
                ]
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            reasons: list[dict[str, Any]] = exc.response.get("CancellationReasons", [])  # type: ignore[assignment]
            codes = [r.get("Code") for r in reasons]
            if codes and codes[0] == "ConditionalCheckFailed":
                return "duplicate"
            return "stale"
        return "stored"

    def list_accounts(self) -> list[AccountEvent]:
        """All accounts, newest first. A scan is fine at demo scale (one account)."""
        events: list[AccountEvent] = []
        paginator = self._client.get_paginator("scan")
        for page in paginator.paginate(
            TableName=self._table,
            FilterExpression="begins_with(PK, :p)",
            ExpressionAttributeValues={":p": {"S": "ACCOUNT#"}},
        ):
            for item in page.get("Items", []):
                events.append(parse_event(json.loads(item["event"]["S"])))
        return sorted(events, key=lambda e: e.occurred_at, reverse=True)
