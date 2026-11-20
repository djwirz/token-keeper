"""``scripts/account_ops.py``: the RefreshFailed drill's account, and deleting one.

The drill must make exactly what the alarm watches (one RefreshFailed, through the
real worker), and the delete must leave nothing behind and touch no one else.
"""

from __future__ import annotations

import importlib.util
import json
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import boto3
import httpx
import pytest
import respx

from receiver.handler import handler as receiver_handler
from tests.conftest import RECEIVER_WEBHOOK_URL, TABLE, TK_TABLE, TokenKeeperEnv, http_event
from tests.github_fakes import mock_events, mock_github
from tests.test_metrics import emitted
from tests.tk_flow import (
    account_item,
    connect_and_callback,
    drain,
    message_bodies,
    purge,
    run_worker,
    scan_table,
)
from token_keeper import metrics
from token_keeper.handlers import _shared
from token_keeper.store import Store


def _script() -> ModuleType:
    path = Path(__file__).resolve().parent.parent / "scripts" / "account_ops.py"
    spec = importlib.util.spec_from_file_location("account_ops", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ops = _script()


@pytest.fixture
def gh() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


def _create(tk_env: TokenKeeperEnv) -> str:
    account_id: str = ops.create_synthetic(Store(TK_TABLE), _shared.crypto(), int(time.time()))
    return account_id


def _receiver_rows() -> list[str]:
    items = boto3.client("dynamodb").scan(TableName=TABLE)["Items"]
    return [i["PK"]["S"] for i in items]


def test_the_fake_tokens_look_like_githubs(tk_env: TokenKeeperEnv) -> None:
    # Token-shaped, so the scrubber and the token-in-logs checks treat them like real ones.
    assert ops.FAKE_ACCESS.startswith("ghu_") and ops.FAKE_REFRESH.startswith("ghr_")


def test_the_drill_account_is_due_now_and_needs_a_refresh(tk_env: TokenKeeperEnv) -> None:
    account_id = _create(tk_env)
    item = account_item(account_id)
    now = int(time.time())
    assert item["state"] == {"S": "active"}
    assert int(item["next_due_at"]["N"]) <= now
    assert int(item["access_expires_at"]["N"]) <= now
    assert int(item["refresh_expires_at"]["N"]) > now + 30 * 24 * 3600
    assert item["external_user_id"] == {"S": ops.DRILL_USER}
    assert item["github_user_id"] == {"N": "0"}
    # The planner would pick it up.
    assert account_id in Store(TK_TABLE).due_accounts(now)


def test_the_drill_fires_refresh_failed_through_the_real_worker(
    gh: respx.MockRouter, tk_env: TokenKeeperEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    token, user = mock_github(gh)
    # What GitHub says to a refresh token it never issued (spec §5).
    token.mock(return_value=httpx.Response(200, json={"error": "bad_refresh_token"}))
    account_id = _create(tk_env)
    capsys.readouterr()

    run_worker(account_id)

    assert user.call_count == 0
    item = account_item(account_id)
    assert item["state"] == {"S": "needs_reauth"}
    assert item["state_reason"] == {"S": "refresh_rejected"}
    assert emitted(capsys).counts[metrics.REFRESH_FAILED] == 1
    assert message_bodies(tk_env.queue_url)[0]["account_id"] == account_id


def test_delete_removes_every_item_and_the_receivers_row(
    gh: respx.MockRouter, tk_env: TokenKeeperEnv
) -> None:
    token, _ = mock_github(gh)
    token.mock(return_value=httpx.Response(200, json={"error": "bad_refresh_token"}))
    gh.post(RECEIVER_WEBHOOK_URL).mock(side_effect=_to_receiver)
    account_id = _create(tk_env)
    run_worker(account_id)  # the drill as it runs live: a needs_reauth event
    drain(tk_env.queue_url)
    assert f"ACCOUNT#{account_id}" in _receiver_rows()

    deleted = Store(TK_TABLE).delete_account(account_id)

    assert sorted(deleted) == sorted(
        [
            f"ACCOUNT#{account_id} META",
            f"ACCOUNT#{account_id} SNAPSHOT",
            f"LINK#demo#{ops.DRILL_USER} META",
            "GHUSER#0 META",
        ]
    )
    assert scan_table(TK_TABLE) == []
    assert ops.delete_receiver_row(boto3.client("dynamodb"), TABLE, account_id)
    assert _receiver_rows() == []
    page = str(receiver_handler(http_event("GET", "/"), None)["body"])
    assert ops.DRILL_LOGIN not in page
    # Twice is harmless.
    assert Store(TK_TABLE).delete_account(account_id) == []


def test_delete_leaves_other_accounts_alone(gh: respx.MockRouter, tk_env: TokenKeeperEnv) -> None:
    mock_github(gh)
    mock_events(gh)
    connect_and_callback()  # Dan's account
    before = {(i["PK"]["S"], i["SK"]["S"]) for i in scan_table(TK_TABLE)}
    account_id = _create(tk_env)

    Store(TK_TABLE).delete_account(account_id)

    after = {(i["PK"]["S"], i["SK"]["S"]) for i in scan_table(TK_TABLE)}
    assert after == before


def test_delete_keeps_a_link_that_moved_to_another_account(
    gh: respx.MockRouter, tk_env: TokenKeeperEnv
) -> None:
    account_id = _create(tk_env)
    # Point the GitHub link elsewhere, as a reconnect under another account would.
    boto3.client("dynamodb").put_item(
        TableName=TK_TABLE,
        Item={"PK": {"S": "GHUSER#0"}, "SK": {"S": "META"}, "account_id": {"S": "acc_other"}},
    )
    deleted = Store(TK_TABLE).delete_account(account_id)
    assert "GHUSER#0 META" not in deleted
    remaining = {(i["PK"]["S"], i["SK"]["S"]) for i in scan_table(TK_TABLE)}
    assert remaining == {("GHUSER#0", "META")}


def test_a_worker_mid_sync_cannot_bring_a_deleted_account_back(
    gh: respx.MockRouter, tk_env: TokenKeeperEnv
) -> None:
    # Delete between the lease and the final write: nothing is recreated.
    from token_keeper.handlers import worker
    from token_keeper.store import LeaseLost

    mock_github(gh)
    mock_events(gh)
    connect_and_callback()
    account_id = Store(TK_TABLE).account_for_user("demo", "u_demo")
    assert account_id
    purge(tk_env.queue_url)
    leased = Store(TK_TABLE).take_lease(account_id, "wrk_test", int(time.time()))
    assert leased is not None
    Store(TK_TABLE).delete_account(account_id)

    with pytest.raises(LeaseLost):
        worker._Sync(leased, "cor_test").run()
    assert scan_table(TK_TABLE) == []


def _to_receiver(request: httpx.Request) -> httpx.Response:
    event = http_event(
        "POST",
        "/webhooks/token-keeper",
        body=request.content.decode(),
        headers=dict(request.headers),
    )
    return httpx.Response(int(receiver_handler(event, None)["statusCode"]))


def test_the_script_never_prints_the_aws_account_id() -> None:
    mask = ops.Masker("123456789012")
    assert mask(json.dumps({"arn": "arn:aws:dynamodb:us-east-1:123456789012:table/x"})) == (
        '{"arn": "arn:aws:dynamodb:us-east-1:<account>:table/x"}'
    )


def test_a_reconnect_with_another_login_mid_delete_cancels_it(
    gh: respx.MockRouter, tk_env: TokenKeeperEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PR 9 review: the reconnect lands between delete_account's read and its
    # transaction, moving the GitHub link from user 1234 to 5678. Without the version
    # condition, GHUSER#5678 was left pointing at a deleted account, and that GitHub
    # user could never connect again (already_linked).
    from tests.github_fakes import user_body
    from token_keeper.store import WriteConflict

    mock_github(gh)
    mock_events(gh)
    connect_and_callback()
    store = Store(TK_TABLE)
    account_id = store.account_for_user("demo", "u_demo")
    assert account_id

    real = Store._linked_account
    fired: list[str] = []

    def reconnect_first(self: Store, pk: str) -> str | None:
        if not fired:
            fired.append(pk)
            mock_github(gh, user=user_body(user_id=5678, login="octo-other"))
            connect_and_callback()
        return real(self, pk)

    monkeypatch.setattr(Store, "_linked_account", reconnect_first)
    with pytest.raises(WriteConflict):
        store.delete_account(account_id)
    monkeypatch.setattr(Store, "_linked_account", real)

    # Nothing was deleted, and the new link still points at a live account.
    assert store.account_for_github_user(5678) == account_id
    assert account_item(account_id)["github_user_id"] == {"N": "5678"}
    # Run again: everything goes, the moved link included.
    deleted = store.delete_account(account_id)
    assert "GHUSER#5678 META" in deleted
    assert scan_table(TK_TABLE) == []
