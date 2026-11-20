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
    {"AttributeName": "state", "AttributeType": "S"},
    {"AttributeName": "next_due_at", "AttributeType": "N"},
]
TTL_ATTRIBUTE = "ttl"
# The planner's index (spec §4): active accounts by when they're due. Keys only, so no
# ciphertext is copied into it. needs_reauth accounts have no next_due_at, so they drop out.
DUE_INDEX = "due"
GLOBAL_SECONDARY_INDEXES = [
    {
        "IndexName": DUE_INDEX,
        "KeySchema": [
            {"AttributeName": "state", "KeyType": "HASH"},
            {"AttributeName": "next_due_at", "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": "KEYS_ONLY"},
    }
]
LEASE_SECONDS = 60


class WriteConflict(Exception):
    """A conditional write lost a race. The caller re-reads and decides again."""


class LeaseLost(Exception):
    """The worker's lease was taken over (reconnect, revocation) or expired.

    The worker discards whatever it holds, including a freshly rotated pair (spec §4).
    """


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


@dataclass(frozen=True)
class LeasedAccount:
    """What the worker's lease write returned: the only way code gets token ciphertext.

    The worker uses these tokens and nothing it read before the lease (spec §4).
    """

    account: Account
    tokens: EncryptedTokens
    owner: str
    lease_until: int
    consecutive_failures: int


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


# needs_reauth deletes the tokens and next_due_at (which takes it out of the due index),
# and keeps the public snapshot so the card still shows who must reconnect (spec §9).
_NEEDS_REAUTH_REMOVE = (
    "access_token_ct, refresh_token_ct, access_expires_at, refresh_expires_at, next_due_at"
)


def _needs_reauth_set(reason: str) -> tuple[str, dict[str, Any]]:
    return (
        "#state = :reauth, state_reason = :reason",
        {":reauth": {"S": "needs_reauth"}, ":reason": {"S": reason}},
    )


def _lease_condition_failed(exc: ClientError) -> bool:
    """True if a transaction was cancelled because its first item's condition failed."""
    if exc.response["Error"]["Code"] != "TransactionCanceledException":
        return False
    reasons = exc.response.get("CancellationReasons") or [{}]
    return reasons[0].get("Code") == "ConditionalCheckFailed"


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
        next_due_at: int,
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
            "next_due_at": {"N": str(next_due_at)},
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
        next_due_at: int,
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
                    "version = :new, next_due_at = :due, consecutive_failures = :zero "
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
                    ":due": {"N": str(next_due_at)},
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

    # Planner -------------------------------------------------------------------

    def due_accounts(self, now: int, page_size: int | None = None) -> list[str]:
        """Ids of active accounts whose ``next_due_at`` has passed (the ``due`` index)."""
        ids: list[str] = []
        kwargs: dict[str, Any] = {
            "TableName": self._table,
            "IndexName": DUE_INDEX,
            "KeyConditionExpression": "#state = :active AND next_due_at <= :now",
            "ExpressionAttributeNames": {"#state": "state"},
            "ExpressionAttributeValues": {":active": {"S": "active"}, ":now": {"N": str(now)}},
        }
        if page_size:
            kwargs["Limit"] = page_size
        while True:
            response = self._client.query(**kwargs)
            ids += [item["PK"]["S"].removeprefix("ACCOUNT#") for item in response["Items"]]
            last = response.get("LastEvaluatedKey")
            if not last:
                return ids
            kwargs["ExclusiveStartKey"] = last

    def push_due(self, account_id: str, now: int, until: int) -> bool:
        """Move ``next_due_at`` ahead so the next planner run doesn't enqueue it again.

        False if it's no longer due or active (another run, or a state change, got there).
        """
        try:
            self._client.update_item(
                TableName=self._table,
                Key=_key(account_pk(account_id)),
                UpdateExpression="SET next_due_at = :until, version = version + :one",
                ConditionExpression="#state = :active AND next_due_at <= :now",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":until": {"N": str(until)},
                    ":one": {"N": "1"},
                    ":active": {"S": "active"},
                    ":now": {"N": str(now)},
                },
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    # Worker: the lease (spec §4 "Writes and the lease") --------------------------

    def take_lease(self, account_id: str, owner: str, now: int) -> LeasedAccount | None:
        """Lease an active account for ``LEASE_SECONDS``. None if it's leased or not active."""
        lease_until = now + LEASE_SECONDS
        try:
            response = self._client.update_item(
                TableName=self._table,
                Key=_key(account_pk(account_id)),
                UpdateExpression=(
                    "SET lease_owner = :me, lease_until = :until, version = version + :one"
                ),
                ConditionExpression=(
                    "(attribute_not_exists(lease_until) OR lease_until < :now) AND #state = :active"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":me": {"S": owner},
                    ":until": {"N": str(lease_until)},
                    ":one": {"N": "1"},
                    ":now": {"N": str(now)},
                    ":active": {"S": "active"},
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise
        item = response["Attributes"]
        return LeasedAccount(
            account=_account(item),
            tokens=EncryptedTokens(
                access_token_ct=bytes(item["access_token_ct"]["B"]),
                refresh_token_ct=bytes(item["refresh_token_ct"]["B"]),
                access_expires_at=int(item["access_expires_at"]["N"]),
                refresh_expires_at=int(item["refresh_expires_at"]["N"]),
            ),
            owner=owner,
            lease_until=lease_until,
            consecutive_failures=int(item.get("consecutive_failures", {"N": "0"})["N"]),
        )

    def _leased_update(
        self,
        leased: LeasedAccount,
        set_clause: str,
        values: dict[str, Any],
        remove: str = "",
        names: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """An Account update conditional on still holding the lease; bumps version."""
        update: dict[str, Any] = {
            "TableName": self._table,
            "Key": _key(account_pk(leased.account.account_id)),
            "UpdateExpression": f"SET {set_clause}, version = version + :one"
            + (f" REMOVE {remove}" if remove else ""),
            "ConditionExpression": "lease_owner = :me",
            "ExpressionAttributeValues": {**values, ":me": {"S": leased.owner}, ":one": {"N": "1"}},
        }
        if names:
            update["ExpressionAttributeNames"] = names
        return update

    def _leased_write(self, update: dict[str, Any]) -> None:
        try:
            self._client.update_item(**update)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise LeaseLost from None
            raise

    def store_refreshed_tokens(self, leased: LeasedAccount, tokens: EncryptedTokens) -> None:
        """Persist a rotated pair before it's used (spec §5). Keeps the lease."""
        self._leased_write(
            self._leased_update(
                leased,
                "access_token_ct = :access_ct, refresh_token_ct = :refresh_ct, "
                "access_expires_at = :access_exp, refresh_expires_at = :refresh_exp",
                _token_values(tokens),
            )
        )

    def finish_sync(
        self,
        leased: LeasedAccount,
        *,
        login: str,
        snapshot: dict[str, Any],
        next_due_at: int,
    ) -> None:
        """A successful sync: new snapshot, failures reset, lease cleared."""
        update = self._leased_update(
            leased,
            "login = :login, next_due_at = :due, consecutive_failures = :zero",
            {
                ":login": {"S": login},
                ":due": {"N": str(next_due_at)},
                ":zero": {"N": "0"},
            },
            remove="lease_owner, lease_until, last_error_code",
        )
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {"Update": update},  # type: ignore[typeddict-item]
                    {
                        "Put": {
                            "TableName": self._table,
                            "Item": _snapshot_item(leased.account.account_id, snapshot),
                        }
                    },
                ]
            )
        except ClientError as exc:
            if _lease_condition_failed(exc):
                raise LeaseLost from None
            # A throttle or a transaction conflict isn't a lost lease: fail the message
            # so SQS retries it, rather than dropping the sync (PR 4 review).
            raise

    def finish_transient(self, leased: LeasedAccount, *, error_code: str, next_due_at: int) -> None:
        """A transient failure: stays active, backs off, no event (spec §7)."""
        self._leased_write(
            self._leased_update(
                leased,
                "last_error_code = :err, next_due_at = :due, "
                "consecutive_failures = consecutive_failures + :one",
                {":err": {"S": error_code}, ":due": {"N": str(next_due_at)}},
                remove="lease_owner, lease_until",
            )
        )

    def finish_needs_reauth(self, leased: LeasedAccount, *, reason: str) -> None:
        """Move to needs_reauth under the lease: tokens deleted, out of the due index."""
        self._leased_write(
            self._leased_update(
                leased,
                *_needs_reauth_set(reason),
                remove=f"{_NEEDS_REAUTH_REMOVE}, lease_owner, lease_until",
            )
            | {"ExpressionAttributeNames": {"#state": "state"}}
        )

    def mark_needs_reauth_takeover(
        self, account_id: str, *, reason: str, delivery: str | None = None
    ) -> bool:
        """The "everyone else" write (spec §4), used by the revocation webhook.

        Ignores any lease and clears it, so a worker mid-sync fails its final write.

        It writes nothing, and returns False, when the account is missing or the write
        would repeat itself (ADR 0018):
        - the account is already ``needs_reauth`` for this ``reason``, or
        - ``delivery`` (GitHub's ``X-GitHub-Delivery`` GUID, which a redelivery keeps)
          is one this account has already acted on. ``revoked_deliveries`` is a string
          set of every one, not just the last: after revoke (A), reconnect, revoke (B),
          reconnect, a redelivery of A must still do nothing (PR 6 review). It grows
          by one id per real revocation, which is a handful per account at most.

        So a redelivery doesn't bump ``version``. True if it wrote.
        """
        set_clause, values = _needs_reauth_set(reason)
        condition = "attribute_exists(PK) AND NOT (#state = :reauth AND state_reason = :reason)"
        add_clause = ""
        if delivery:
            add_clause = "ADD revoked_deliveries :delivery_set "
            # contains() on a missing attribute is false, so a first revocation passes.
            condition += " AND NOT contains(revoked_deliveries, :delivery)"
            values = {
                **values,
                ":delivery": {"S": delivery},
                ":delivery_set": {"SS": [delivery]},
            }
        try:
            self._client.update_item(
                TableName=self._table,
                Key=_key(account_pk(account_id)),
                UpdateExpression=f"SET {set_clause}, version = version + :one "
                f"{add_clause}REMOVE {_NEEDS_REAUTH_REMOVE}, lease_owner, lease_until",
                ConditionExpression=condition,
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={**values, ":one": {"N": "1"}},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def _transact(self, items: list[TransactWriteItemTypeDef]) -> None:
        try:
            self._client.transact_write_items(TransactItems=items)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "TransactionCanceledException":
                raise WriteConflict from None
            raise
