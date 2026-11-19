"""Token Keeper's single DynamoDB table (spec §4, ADR 0013).

Every item has ``PK`` and ``SK`` (both strings):

| Item        | PK                                 | SK         |
|-------------|------------------------------------|------------|
| Account     | ``ACCOUNT#<account_id>``           | ``META``   |
| Snapshot    | ``ACCOUNT#<account_id>``           | ``SNAPSHOT`` |
| OAuth state | ``STATE#<sha256 of state>``        | ``META``   |
| User link   | ``LINK#<app_id>#<external_user_id>`` | ``META`` |
| GitHub link | ``GHUSER#<github_user_id>``        | ``META``   |

Times are epoch seconds (N), ciphertext is binary (B). State items carry ``ttl``.
Every change to an Account bumps ``version`` with a conditional write (spec §4).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import boto3
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import TransactWriteItemTypeDef

META = "META"
SNAPSHOT = "SNAPSHOT"
STATE_TTL_SECONDS = 10 * 60

# Shared with the local runner and tests; checked against CDK by tests/test_infra.py.
KEY_SCHEMA = [
    {"AttributeName": "PK", "KeyType": "HASH"},
    {"AttributeName": "SK", "KeyType": "RANGE"},
]
ATTRIBUTE_DEFINITIONS = [
    {"AttributeName": "PK", "AttributeType": "S"},
    {"AttributeName": "SK", "AttributeType": "S"},
]
TTL_ATTRIBUTE = "ttl"


class WriteConflict(Exception):
    """A conditional write lost a race. The caller re-reads and decides again."""


def account_pk(account_id: str) -> str:
    return f"ACCOUNT#{account_id}"


def _key(pk: str, sk: str = META) -> dict[str, Any]:
    return {"PK": {"S": pk}, "SK": {"S": sk}}


def _link_pk(app_id: str, external_user_id: str) -> str:
    return f"LINK#{app_id}#{external_user_id}"


def _github_pk(github_user_id: int) -> str:
    return f"GHUSER#{github_user_id}"


@dataclass(frozen=True)
class StateRecord:
    app_id: str
    external_user_id: str
    return_to: str
    correlation_id: str
    pkce_verifier_ct: bytes
    expires_at: int


@dataclass(frozen=True)
class Account:
    """An Account item without its token fields (only the worker reads those)."""

    account_id: str
    app_id: str
    external_user_id: str
    github_user_id: int
    login: str
    state: str
    state_reason: str | None
    version: int


@dataclass(frozen=True)
class EncryptedTokens:
    access_token_ct: bytes
    refresh_token_ct: bytes
    access_expires_at: int
    refresh_expires_at: int


@dataclass(frozen=True)
class Identity:
    github_user_id: int
    login: str


def _account(item: dict[str, Any]) -> Account:
    reason = item.get("state_reason")
    return Account(
        account_id=item["PK"]["S"].removeprefix("ACCOUNT#"),
        app_id=item["app_id"]["S"],
        external_user_id=item["external_user_id"]["S"],
        github_user_id=int(item["github_user_id"]["N"]),
        login=item["login"]["S"],
        state=item["state"]["S"],
        state_reason=reason["S"] if reason else None,
        version=int(item["version"]["N"]),
    )


def _snapshot_item(account_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    return {**_key(account_pk(account_id), SNAPSHOT), "snapshot": {"S": json.dumps(snapshot)}}


def _token_values(tokens: EncryptedTokens) -> dict[str, Any]:
    return {
        ":access_ct": {"B": tokens.access_token_ct},
        ":refresh_ct": {"B": tokens.refresh_token_ct},
        ":access_exp": {"N": str(tokens.access_expires_at)},
        ":refresh_exp": {"N": str(tokens.refresh_expires_at)},
    }


class Store:
    def __init__(self, table_name: str, client: DynamoDBClient | None = None) -> None:
        self._table = table_name
        self._client: DynamoDBClient = client or boto3.client("dynamodb")

    # OAuth state ---------------------------------------------------------------

    def put_state(self, hashed_state: str, record: StateRecord) -> None:
        self._client.put_item(
            TableName=self._table,
            Item={
                **_key(f"STATE#{hashed_state}"),
                "app_id": {"S": record.app_id},
                "external_user_id": {"S": record.external_user_id},
                "return_to": {"S": record.return_to},
                "correlation_id": {"S": record.correlation_id},
                "pkce_verifier_ct": {"B": record.pkce_verifier_ct},
                TTL_ATTRIBUTE: {"N": str(record.expires_at)},
            },
            ConditionExpression="attribute_not_exists(PK)",
        )

    def take_state(self, hashed_state: str, now: int) -> StateRecord | None:
        """Delete the state record and return it, so it can be used only once.

        DynamoDB's TTL deletes lazily, so expiry is checked here too.
        """
        response = self._client.delete_item(
            TableName=self._table, Key=_key(f"STATE#{hashed_state}"), ReturnValues="ALL_OLD"
        )
        item = response.get("Attributes")
        if not item:
            return None
        expires_at = int(item[TTL_ATTRIBUTE]["N"])
        if expires_at <= now:
            return None
        return StateRecord(
            app_id=item["app_id"]["S"],
            external_user_id=item["external_user_id"]["S"],
            return_to=item["return_to"]["S"],
            correlation_id=item["correlation_id"]["S"],
            pkce_verifier_ct=bytes(item["pkce_verifier_ct"]["B"]),
            expires_at=expires_at,
        )

    # Links and accounts --------------------------------------------------------

    def _linked_account(self, pk: str) -> str | None:
        response = self._client.get_item(TableName=self._table, Key=_key(pk), ConsistentRead=True)
        item = response.get("Item")
        return item["account_id"]["S"] if item else None

    def account_for_user(self, app_id: str, external_user_id: str) -> str | None:
        return self._linked_account(_link_pk(app_id, external_user_id))

    def account_for_github_user(self, github_user_id: int) -> str | None:
        return self._linked_account(_github_pk(github_user_id))

    def get_account(self, account_id: str) -> Account | None:
        response = self._client.get_item(
            TableName=self._table, Key=_key(account_pk(account_id)), ConsistentRead=True
        )
        item = response.get("Item")
        return _account(item) if item else None

    def get_account_and_snapshot(
        self, account_id: str
    ) -> tuple[Account, dict[str, Any] | None] | None:
        """The Account (without tokens) and its snapshot, read together."""
        response = self._client.query(
            TableName=self._table,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": {"S": account_pk(account_id)}},
            ProjectionExpression=(
                "PK, SK, app_id, external_user_id, github_user_id, login, "
                "#state, state_reason, version, #snapshot"
            ),
            # Both are DynamoDB reserved words.
            ExpressionAttributeNames={"#state": "state", "#snapshot": "snapshot"},
            ConsistentRead=True,
        )
        items = {item["SK"]["S"]: item for item in response.get("Items", [])}
        if META not in items:
            return None
        snapshot_item = items.get(SNAPSHOT)
        snapshot = json.loads(snapshot_item["snapshot"]["S"]) if snapshot_item else None
        return _account(items[META]), snapshot

    def create_account(
        self,
        *,
        account_id: str,
        app_id: str,
        external_user_id: str,
        identity: Identity,
        tokens: EncryptedTokens,
        snapshot: dict[str, Any],
        now: int,
    ) -> None:
        """Account, User link, GitHub link and Snapshot, all or nothing."""
        account_item = {
            **_key(account_pk(account_id)),
            "app_id": {"S": app_id},
            "external_user_id": {"S": external_user_id},
            "github_user_id": {"N": str(identity.github_user_id)},
            "login": {"S": identity.login},
            "state": {"S": "active"},
            "version": {"N": "1"},
            "next_due_at": {"N": str(now)},
            "access_token_ct": {"B": tokens.access_token_ct},
            "refresh_token_ct": {"B": tokens.refresh_token_ct},
            "access_expires_at": {"N": str(tokens.access_expires_at)},
            "refresh_expires_at": {"N": str(tokens.refresh_expires_at)},
            "consecutive_failures": {"N": "0"},
        }
        link = {"account_id": {"S": account_id}}
        new = "attribute_not_exists(PK)"
        self._transact(
            [
                {
                    "Put": {
                        "TableName": self._table,
                        "Item": account_item,
                        "ConditionExpression": new,
                    }
                },
                {
                    "Put": {
                        "TableName": self._table,
                        "Item": {**_key(_link_pk(app_id, external_user_id)), **link},
                        "ConditionExpression": new,
                    }
                },
                {
                    "Put": {
                        "TableName": self._table,
                        "Item": {**_key(_github_pk(identity.github_user_id)), **link},
                        "ConditionExpression": new,
                    }
                },
                {"Put": {"TableName": self._table, "Item": _snapshot_item(account_id, snapshot)}},
            ]
        )

    def reconnect_account(
        self,
        account: Account,
        *,
        identity: Identity,
        tokens: EncryptedTokens,
        snapshot: dict[str, Any],
        now: int,
    ) -> int:
        """Take over an existing account with a fresh grant (spec §4 "Everyone else").

        Conditional on the version that was read; clears any worker's lease, so a sync
        in flight fails its final write and discards what it holds. A different GitHub
        account replaces the old one's GitHub link. Returns the new version.
        """
        new_version = account.version + 1
        update: TransactWriteItemTypeDef = {
            "Update": {
                "TableName": self._table,
                "Key": _key(account_pk(account.account_id)),
                "UpdateExpression": (
                    "SET access_token_ct = :access_ct, refresh_token_ct = :refresh_ct, "
                    "access_expires_at = :access_exp, refresh_expires_at = :refresh_exp, "
                    "github_user_id = :gh, login = :login, #state = :active, "
                    "version = :new, next_due_at = :now, consecutive_failures = :zero "
                    "REMOVE state_reason, lease_owner, lease_until, last_error_code"
                ),
                "ConditionExpression": "version = :old",
                "ExpressionAttributeNames": {"#state": "state"},
                "ExpressionAttributeValues": {
                    **_token_values(tokens),
                    ":gh": {"N": str(identity.github_user_id)},
                    ":login": {"S": identity.login},
                    ":active": {"S": "active"},
                    ":new": {"N": str(new_version)},
                    ":old": {"N": str(account.version)},
                    ":now": {"N": str(now)},
                    ":zero": {"N": "0"},
                },
            }
        }
        items: list[TransactWriteItemTypeDef] = [
            update,
            {
                "Put": {
                    "TableName": self._table,
                    "Item": _snapshot_item(account.account_id, snapshot),
                }
            },
        ]
        if identity.github_user_id != account.github_user_id:
            items += [
                {
                    "Delete": {
                        "TableName": self._table,
                        "Key": _key(_github_pk(account.github_user_id)),
                        "ConditionExpression": "attribute_not_exists(PK) OR account_id = :me",
                        "ExpressionAttributeValues": {":me": {"S": account.account_id}},
                    }
                },
                {
                    "Put": {
                        "TableName": self._table,
                        "Item": {
                            **_key(_github_pk(identity.github_user_id)),
                            "account_id": {"S": account.account_id},
                        },
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                },
            ]
        self._transact(items)
        return new_version

    def _transact(self, items: list[TransactWriteItemTypeDef]) -> None:
        try:
            self._client.transact_write_items(TransactItems=items)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "TransactionCanceledException":
                raise WriteConflict from None
            raise
