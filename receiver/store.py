"""The receiver's table: the latest event per account.

Items:
- ``PK=ACCOUNT#<account_id>``: ``version`` (N), ``event`` (S, the event as JSON)

Every event carries the account's full state and a ``version``, so ``version`` is the
only deduplication needed (ADR 0012): an event is stored only if its version is higher
than the stored one. A redelivery, or any older or equal version, changes nothing.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

import boto3
from botocore.exceptions import ClientError

from token_keeper.contract.events import AccountEvent, parse_event

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient

SaveResult = Literal["stored", "stale"]

# Shared with the local runner and checked against CDK by tests/test_infra.py.
KEY_SCHEMA = [{"AttributeName": "PK", "KeyType": "HASH"}]
ATTRIBUTE_DEFINITIONS = [{"AttributeName": "PK", "AttributeType": "S"}]


class Store:
    def __init__(self, table_name: str, client: DynamoDBClient | None = None) -> None:
        self._table = table_name
        self._client: DynamoDBClient = client or boto3.client("dynamodb")

    def save_event(self, event: AccountEvent) -> SaveResult:
        """Store the event if it's newer than what's stored; otherwise do nothing."""
        try:
            self._client.put_item(
                TableName=self._table,
                Item={
                    "PK": {"S": f"ACCOUNT#{event.account_id}"},
                    "version": {"N": str(event.version)},
                    "event": {"S": json.dumps(event.to_dict())},
                },
                ConditionExpression="attribute_not_exists(PK) OR version < :v",
                ExpressionAttributeValues={":v": {"N": str(event.version)}},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            return "stale"
        return "stored"

    def list_accounts(self) -> list[AccountEvent]:
        """All accounts, newest first. A scan is fine at demo scale (one account)."""
        events: list[AccountEvent] = []
        paginator = self._client.get_paginator("scan")
        for page in paginator.paginate(TableName=self._table):
            for item in page.get("Items", []):
                events.append(parse_event(json.loads(item["event"]["S"])))
        return sorted(events, key=lambda e: e.occurred_at, reverse=True)
