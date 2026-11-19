"""GET /callback: state, code exchange, account writes, outcomes (spec §6.2, ADR 0011)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from urllib.parse import parse_qs, urlsplit

import boto3
import httpx
import pytest
import respx

from tests.conftest import (
    GITHUB_CLIENT_ID,
    GITHUB_CLIENT_SECRET,
    PUBLIC_URL,
    TK_PUBLIC_URL,
    TK_TABLE,
    TokenKeeperEnv,
)
from tests.github_fakes import (
    ACCESS_TOKEN,
    REFRESH_TOKEN,
    SECOND_ACCESS_TOKEN,
    mock_github,
    token_body,
    user_body,
)
from tests.tk_flow import (
    connect_and_callback,
    queue_messages,
    run_callback,
    scan_table,
    start_connect,
)
from token_keeper import crypto
from token_keeper.handlers import _shared, callback
from token_keeper.store import Store

pytestmark = pytest.mark.usefixtures("tk_env")


@pytest.fixture
def gh() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


def _items() -> dict[tuple[str, str], dict[str, object]]:
    return {(i["PK"]["S"], i["SK"]["S"]): i for i in scan_table(TK_TABLE)}


def _result(response: dict[str, object]) -> str:
    assert response["statusCode"] == 302
    location = str(response["headers"]["location"])  # type: ignore[index]
    assert location.startswith(PUBLIC_URL + "?")
    return parse_qs(urlsplit(location).query)["result"][0]


def _account_ids() -> list[str]:
    return [
        pk.removeprefix("ACCOUNT#")
        for pk, sk in _items()
        if sk == "META" and pk.startswith("ACCOUNT#")
    ]


def test_new_connection(gh: respx.MockRouter, tk_env: TokenKeeperEnv) -> None:
    token_route, user_route = mock_github(gh)
    _, params = start_connect()
    assert _result(run_callback({"code": "the-code", "state": params["state"]})) == "connected"

    # The code exchange: PKCE verifier, redirect_uri, JSON accept header.
    sent = parse_qs(token_route.calls.last.request.content.decode())
    assert sent["code"] == ["the-code"]
    assert sent["client_id"] == [GITHUB_CLIENT_ID]
    assert sent["client_secret"] == [GITHUB_CLIENT_SECRET]
    assert sent["redirect_uri"] == [f"{TK_PUBLIC_URL}/callback"]
    assert len(sent["code_verifier"][0]) >= 43
    assert token_route.calls.last.request.headers["accept"] == "application/json"
    assert user_route.calls.last.request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"

    items = _items()
    (account_id,) = _account_ids()
    assert account_id.startswith("acc_")
    account = items[(f"ACCOUNT#{account_id}", "META")]
    assert account["state"] == {"S": "active"}
    assert account["version"] == {"N": "1"}
    assert account["login"] == {"S": "octo-demo"}
    assert "next_due_at" in account
    assert items[("LINK#demo#u_demo", "META")]["account_id"] == {"S": account_id}
    assert items[("GHUSER#1234", "META")]["account_id"] == {"S": account_id}
    snapshot = json.loads(items[(f"ACCOUNT#{account_id}", "SNAPSHOT")]["snapshot"]["S"])  # type: ignore[index]
    assert snapshot["profile"]["name"] == "Octo Demo"
    assert snapshot["recent_events"] == []
    # The state record is gone: single use.
    assert not any(pk.startswith("STATE#") for pk, _ in items)

    # Tokens are ciphertext only, bound to this account.
    assert ACCESS_TOKEN not in str(items) and REFRESH_TOKEN not in str(items)
    assert "ghu_" not in str(items) and "ghr_" not in str(items)
    kms = crypto.Crypto(tk_env.key_id)
    context = crypto.account_context(account_id)
    assert kms.decrypt(bytes(account["access_token_ct"]["B"]), context) == ACCESS_TOKEN  # type: ignore[index]
    assert kms.decrypt(bytes(account["refresh_token_ct"]["B"]), context) == REFRESH_TOKEN  # type: ignore[index]

    # One delivery message: ids only.
    (message,) = queue_messages(tk_env.queue_url)
    body = json.loads(message["Body"])
    assert body["account_id"] == account_id
    assert body["type"] == "account.connected"
    assert body["webhook_id"].startswith("msg_")
    assert message["MessageAttributes"]["correlation_id"]["StringValue"].startswith("cor_")
    assert "ghu_" not in message["Body"]


def test_state_is_single_use(gh: respx.MockRouter) -> None:
    mock_github(gh)
    _, params = start_connect()
    query = {"code": "c", "state": params["state"]}
    assert _result(run_callback(query)) == "connected"
    replay = run_callback(query)
    assert replay["statusCode"] == 400
    assert len(_account_ids()) == 1


def test_expired_state_is_rejected(gh: respx.MockRouter, monkeypatch: pytest.MonkeyPatch) -> None:
    token_route, _ = mock_github(gh)
    _, params = start_connect()
    real_now = _shared.now()
    monkeypatch.setattr(callback, "now", lambda: real_now + 11 * 60)
    assert run_callback({"code": "c", "state": params["state"]})["statusCode"] == 400
    assert not token_route.called


@pytest.mark.parametrize("query", [{"code": "c"}, {"code": "c", "state": "made-up"}, {}])
def test_missing_or_unknown_state_is_the_error_page(
    gh: respx.MockRouter, query: dict[str, str]
) -> None:
    token_route, _ = mock_github(gh)
    response = run_callback(query)
    assert response["statusCode"] == 400
    assert "That didn't work" in str(response["body"])
    assert not token_route.called


def test_user_cancel_on_github_is_denied(gh: respx.MockRouter) -> None:
    token_route, _ = mock_github(gh)
    _, params = start_connect()
    response = run_callback({"error": "access_denied", "state": params["state"]})
    assert _result(response) == "denied"
    assert not token_route.called
    assert scan_table(TK_TABLE) == []  # state consumed, nothing stored


def test_github_error_without_valid_state_is_the_error_page(gh: respx.MockRouter) -> None:
    mock_github(gh)
    assert run_callback({"error": "access_denied", "state": "nope"})["statusCode"] == 400


def test_token_error_in_a_200_body_fails(gh: respx.MockRouter) -> None:
    # GitHub reports a bad code with HTTP 200 and an error field (spec §5).
    mock_github(gh, token={"error": "bad_verification_code", "error_description": "x"})
    response = connect_and_callback()
    assert response["statusCode"] == 502
    assert _account_ids() == []


def test_non_expiring_token_is_refused(gh: respx.MockRouter) -> None:
    body = token_body()
    for key in ("refresh_token", "refresh_token_expires_in", "expires_in"):
        body.pop(key)
    mock_github(gh, token=body)
    assert connect_and_callback()["statusCode"] == 502
    assert _account_ids() == []


def test_github_user_failure(gh: respx.MockRouter) -> None:
    mock_github(gh, user_status=500)
    assert connect_and_callback()["statusCode"] == 502
    assert _account_ids() == []


def test_github_unreachable(gh: respx.MockRouter) -> None:
    gh.post("https://github.com/login/oauth/access_token").mock(
        side_effect=httpx.ConnectTimeout("timeout")
    )
    assert connect_and_callback()["statusCode"] == 502


def test_reconnect_keeps_the_account_and_takes_over(
    gh: respx.MockRouter, tk_env: TokenKeeperEnv
) -> None:
    mock_github(gh)
    assert _result(connect_and_callback()) == "connected"
    (account_id,) = _account_ids()

    # A worker holds a lease and the account had been marked needs_reauth.
    boto3.client("dynamodb").update_item(
        TableName=TK_TABLE,
        Key={"PK": {"S": f"ACCOUNT#{account_id}"}, "SK": {"S": "META"}},
        UpdateExpression="SET lease_owner = :o, lease_until = :u, #s = :n, state_reason = :r",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={
            ":o": {"S": "worker-1"},
            ":u": {"N": "9999999999"},
            ":n": {"S": "needs_reauth"},
            ":r": {"S": "revoked"},
        },
    )
    gh.post("https://github.com/login/oauth/access_token").mock(
        return_value=httpx.Response(200, json=token_body(access=SECOND_ACCESS_TOKEN))
    )
    assert _result(connect_and_callback()) == "connected"

    assert _account_ids() == [account_id]
    account = _items()[(f"ACCOUNT#{account_id}", "META")]
    assert account["version"] == {"N": "2"}
    assert account["state"] == {"S": "active"}
    for gone in ("state_reason", "lease_owner", "lease_until"):
        assert gone not in account
    kms = crypto.Crypto(tk_env.key_id)
    access = kms.decrypt(bytes(account["access_token_ct"]["B"]), crypto.account_context(account_id))  # type: ignore[index]
    assert access == SECOND_ACCESS_TOKEN
    assert len(queue_messages(tk_env.queue_url)) == 2  # account.connected each time


def test_reconnect_with_another_github_account_moves_the_link(gh: respx.MockRouter) -> None:
    mock_github(gh)
    assert _result(connect_and_callback()) == "connected"
    (account_id,) = _account_ids()

    gh.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json=user_body(user_id=5678, login="other-octo"))
    )
    assert _result(connect_and_callback()) == "connected"
    items = _items()
    assert ("GHUSER#1234", "META") not in items
    assert items[("GHUSER#5678", "META")]["account_id"] == {"S": account_id}
    assert items[(f"ACCOUNT#{account_id}", "META")]["login"] == {"S": "other-octo"}


def test_racing_first_connects_end_as_one_account(
    gh: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two first connects for one user: the slower one loses the write and reconnects.

    The account exists, but this callback read "no user link" before the other one
    committed. Its create transaction must fail on the user link, re-read, and become
    a takeover of the winner's account (spec §4, ADR 0011), with its tokens encrypted
    for that account.
    """
    mock_github(gh)
    assert _result(connect_and_callback()) == "connected"
    (account_id,) = _account_ids()

    # The losing callback read before the winner committed: no user link, no GitHub
    # link. Both reads are stale once, then real.
    reads: list[str | None] = []
    real_user, real_github = Store.account_for_user, Store.account_for_github_user

    def stale_user(self: Store, app_id: str, external_user_id: str) -> str | None:
        found = real_user(self, app_id, external_user_id) if reads else None
        reads.append(found)
        return found

    def stale_github(self: Store, github_user_id: int) -> str | None:
        return real_github(self, github_user_id) if len(reads) > 1 else None

    monkeypatch.setattr(Store, "account_for_user", stale_user)
    monkeypatch.setattr(Store, "account_for_github_user", stale_github)
    assert _result(connect_and_callback()) == "connected"

    assert reads == [None, account_id]  # one stale read, a lost write, then the retry
    assert _account_ids() == [account_id]
    account = _items()[(f"ACCOUNT#{account_id}", "META")]
    assert account["version"] == {"N": "2"}


def test_github_account_linked_to_another_user_is_already_linked(
    gh: respx.MockRouter, tk_env: TokenKeeperEnv
) -> None:
    mock_github(gh)
    assert _result(connect_and_callback("u_demo")) == "connected"
    before = scan_table(TK_TABLE)

    assert _result(connect_and_callback("u_someone_else")) == "already_linked"
    after = [i for i in scan_table(TK_TABLE) if not i["PK"]["S"].startswith("STATE#")]
    assert sorted(map(str, after)) == sorted(map(str, before))
    assert len(queue_messages(tk_env.queue_url)) == 1
