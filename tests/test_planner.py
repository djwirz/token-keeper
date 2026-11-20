"""The planner and the due index (spec §4, ADR 0004)."""

from __future__ import annotations

import time
from collections.abc import Iterator

import boto3
import pytest
import respx

from tests.conftest import TK_TABLE, TokenKeeperEnv
from tests.github_fakes import mock_github, user_body
from tests.tk_flow import account_item, connect_and_callback, message_bodies, purge, set_numbers
from token_keeper import sync
from token_keeper.handlers import planner
from token_keeper.store import Store


@pytest.fixture
def gh() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


def _connect(gh: respx.MockRouter, n: int) -> str:
    mock_github(gh, user=user_body(user_id=1000 + n, login=f"octo-{n}"))
    connect_and_callback(external_user_id=f"u_{n}")
    account_id = Store(TK_TABLE).account_for_user("demo", f"u_{n}")
    assert account_id
    return account_id


def test_planner_enqueues_only_due_active_accounts(
    gh: respx.MockRouter, tk_env: TokenKeeperEnv
) -> None:
    due, later, revoked = (_connect(gh, n) for n in range(3))
    purge(tk_env.sync_queue_url)
    now = int(time.time())
    set_numbers(due, next_due_at=now - 10)
    set_numbers(later, next_due_at=now + 1000)
    set_numbers(revoked, next_due_at=now - 10)
    Store(TK_TABLE).mark_needs_reauth_takeover(revoked, reason="revoked")
    before = int(account_item(due)["version"]["N"])

    assert planner.handler({}, None) == {"enqueued": 1}
    assert message_bodies(tk_env.sync_queue_url) == [{"account_id": due}]
    item = account_item(due)
    assert int(item["next_due_at"]["N"]) - now == pytest.approx(sync.PLANNER_PUSH_SECONDS, abs=5)
    assert int(item["version"]["N"]) == before + 1

    # The push means the next run doesn't enqueue it again.
    assert planner.handler({}, None) == {"enqueued": 0}


def test_needs_reauth_accounts_leave_the_due_index(
    gh: respx.MockRouter, tk_env: TokenKeeperEnv
) -> None:
    account_id = _connect(gh, 0)
    Store(TK_TABLE).mark_needs_reauth_takeover(account_id, reason="revoked")
    items = boto3.client("dynamodb").scan(TableName=TK_TABLE, IndexName="due")["Items"]
    assert items == []


def test_the_index_holds_keys_only(gh: respx.MockRouter, tk_env: TokenKeeperEnv) -> None:
    _connect(gh, 0)
    (item,) = boto3.client("dynamodb").scan(TableName=TK_TABLE, IndexName="due")["Items"]
    # No ciphertext is copied into the index.
    assert set(item) == {"PK", "SK", "state", "next_due_at"}


def test_due_accounts_follows_pages(gh: respx.MockRouter, tk_env: TokenKeeperEnv) -> None:
    ids = {_connect(gh, n) for n in range(3)}
    for account_id in ids:
        set_numbers(account_id, next_due_at=int(time.time()) - 10)
    assert set(Store(TK_TABLE).due_accounts(int(time.time()), page_size=1)) == ids


def test_push_due_loses_to_an_earlier_push(gh: respx.MockRouter, tk_env: TokenKeeperEnv) -> None:
    account_id = _connect(gh, 0)
    now = int(time.time())
    set_numbers(account_id, next_due_at=now - 10)
    store = Store(TK_TABLE)
    assert store.push_due(account_id, now, now + 1800)
    assert not store.push_due(account_id, now, now + 1800)
