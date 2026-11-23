"""What the README GIF's checks need to know about the demo account, read-only.

Shared by the local runner's ``GET /account`` control and ``scripts/demo_gif.py``'s
AWS reader, so the rehearsal and the live run check the same facts. Token values are
never read: whether the account still holds tokens is a filtered COUNT, so no
ciphertext leaves DynamoDB.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from token_keeper.store import META, Store, account_pk

APP_ID = "demo"
DEMO_USER = "u_demo"


@dataclass(frozen=True)
class Facts:
    account_id: str
    state: str
    reason: str | None
    version: int
    next_due_at: int | None
    has_tokens: bool
    fetched_at: str | None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Facts:
        return cls(**data)


def read_facts(client: Any, table: str) -> Facts | None:
    """The demo user's account, or ``None`` if it has never connected."""
    store = Store(table, client=client)
    account_id = store.account_for_user(APP_ID, DEMO_USER)
    if account_id is None:
        return None
    found = store.get_account_and_snapshot(account_id)
    if found is None:
        return None
    account, snapshot = found
    key = {":pk": {"S": account_pk(account_id)}, ":meta": {"S": META}}
    due = client.get_item(
        TableName=table,
        Key={"PK": key[":pk"], "SK": key[":meta"]},
        ProjectionExpression="next_due_at",
        ConsistentRead=True,
    ).get("Item", {})
    with_tokens = client.query(
        TableName=table,
        KeyConditionExpression="PK = :pk AND SK = :meta",
        FilterExpression="attribute_exists(access_token_ct) AND attribute_exists(refresh_token_ct)",
        ExpressionAttributeValues=key,
        Select="COUNT",
        ConsistentRead=True,
    )["Count"]
    return Facts(
        account_id=account_id,
        state=account.state,
        reason=account.state_reason,
        version=account.version,
        next_due_at=int(due["next_due_at"]["N"]) if "next_due_at" in due else None,
        has_tokens=with_tokens > 0,
        fetched_at=(snapshot or {}).get("fetched_at"),
    )
