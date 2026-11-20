"""The revocation webhook (spec §6.4, ADR 0010, ADR 0018), on recorded deliveries."""

from __future__ import annotations

import base64
import hmac
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from receiver.handler import _store as receiver_store
from receiver.handler import handler as receiver_handler
from tests.conftest import RECEIVER_WEBHOOK_URL, TK_TABLE, TokenKeeperEnv, http_event
from tests.github_fakes import mock_events, mock_github, user_body
from tests.tk_flow import (
    account_item,
    connect_and_callback,
    drain,
    github_delivery,
    message_bodies,
    purge,
    run_github_webhook,
    scan_table,
)
from token_keeper import github
from token_keeper.store import Store

VECTORS = json.loads((Path(__file__).parent / "vectors" / "github_deliveries.json").read_text())
TOKEN_FIELDS = {"access_token_ct", "refresh_token_ct", "access_expires_at", "refresh_expires_at"}


@pytest.fixture
def gh() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


def _to_receiver(request: httpx.Request) -> httpx.Response:
    event = http_event(
        "POST",
        "/webhooks/token-keeper",
        body=request.content.decode(),
        headers=dict(request.headers),
    )
    return httpx.Response(int(receiver_handler(event, None)["statusCode"]))


@pytest.fixture
def account(gh: respx.MockRouter, tk_env: TokenKeeperEnv) -> str:
    """Dan's account (GitHub user 1234, as in the recorded delivery), delivered once."""
    mock_github(gh)
    mock_events(gh)
    gh.post(RECEIVER_WEBHOOK_URL).mock(side_effect=_to_receiver)
    connect_and_callback()
    account_id = Store(TK_TABLE).account_for_user("demo", "u_demo")
    assert account_id
    purge(tk_env.sync_queue_url)
    assert drain(tk_env.queue_url) == 1  # account.connected reaches the card
    return account_id


def _page() -> str:
    return str(receiver_handler(http_event("GET", "/"), None)["body"])


def _version(account_id: str) -> int:
    return int(account_item(account_id)["version"]["N"])


def _assert_untouched(account_id: str, version: int, tk_env: TokenKeeperEnv) -> None:
    item = account_item(account_id)
    assert item["state"] == {"S": "active"}
    assert set(item) >= TOKEN_FIELDS
    assert _version(account_id) == version
    assert message_bodies(tk_env.queue_url) == []


# The signature, checked against GitHub's own example ------------------------------


def test_githubs_published_signature_example_verifies() -> None:
    vector = VECTORS["signature_vector"]
    github.verify_webhook_signature(vector["secret"], vector["header"], vector["payload"].encode())


@pytest.mark.parametrize(
    "header",
    [
        # GitHub sends lowercase hex after "sha256=": these are near misses.
        lambda h: h.upper(),
        lambda h: h.removeprefix("sha256="),
        lambda h: "sha1=" + h.removeprefix("sha256="),
        lambda h: "sha256=" + base64.b64encode(bytes.fromhex(h[7:])).decode(),
        lambda h: h[:-1] + ("0" if h[-1] != "0" else "1"),
    ],
)
def test_near_misses_of_githubs_example_fail(header: Any) -> None:
    vector = VECTORS["signature_vector"]
    with pytest.raises(github.WebhookSignatureError, match="bad_signature"):
        github.verify_webhook_signature(
            vector["secret"], header(vector["header"]), vector["payload"].encode()
        )


def test_the_comparison_is_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    vector = VECTORS["signature_vector"]
    github.verify_webhook_signature(vector["secret"], vector["header"], vector["payload"].encode())
    assert len(calls) == 1


# Revocation ----------------------------------------------------------------------


def test_a_revocation_flips_the_card(account: str, tk_env: TokenKeeperEnv) -> None:
    before = _version(account)
    assert run_github_webhook(github_delivery()) == 204

    item = account_item(account)
    assert item["state"] == {"S": "needs_reauth"}
    assert item["state_reason"] == {"S": "revoked"}
    assert not TOKEN_FIELDS & set(item)
    assert not {"next_due_at", "lease_owner", "lease_until"} & set(item)
    assert _version(account) == before + 1
    assert [m["type"] for m in message_bodies(tk_env.queue_url)] == ["account.updated"]

    assert drain(tk_env.queue_url) == 1
    assert "Needs re-auth (revoked)" in _page()


def test_a_redelivery_writes_nothing_and_the_receiver_ignores_it(
    account: str,
    tk_env: TokenKeeperEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_github_webhook(github_delivery()) == 204
    assert drain(tk_env.queue_url) == 1
    version = _version(account)

    assert run_github_webhook(github_delivery()) == 204  # GitHub keeps the GUID
    assert _version(account) == version
    assert '"github_webhook.already_revoked"' in capsys.readouterr().out
    # The event is sent again (it heals a lost first enqueue); the receiver drops it.
    stored = receiver_store().list_accounts()
    assert drain(tk_env.queue_url) == 1
    # The same version again: the receiver keeps what it had (ADR 0012).
    assert receiver_store().list_accounts() == stored
    assert stored[0].version == version
    assert "Needs re-auth (revoked)" in _page()


def test_a_new_delivery_for_an_already_revoked_account_writes_nothing(
    account: str, tk_env: TokenKeeperEnv
) -> None:
    assert run_github_webhook(github_delivery()) == 204
    version = _version(account)
    other = github_delivery(headers={"X-GitHub-Delivery": "0d6f4b80-0000-11ef-8000-0000000000ff"})
    assert run_github_webhook(other) == 204
    assert _version(account) == version


def test_a_redelivery_after_a_reconnect_leaves_the_new_grant_alone(
    account: str, tk_env: TokenKeeperEnv
) -> None:
    assert run_github_webhook(github_delivery()) == 204
    connect_and_callback()  # Dan reconnects
    assert account_item(account)["state"] == {"S": "active"}
    version = _version(account)

    # Someone presses Redeliver on the old revocation in GitHub's Recent Deliveries.
    assert run_github_webhook(github_delivery()) == 204
    item = account_item(account)
    assert item["state"] == {"S": "active"}
    assert set(item) >= TOKEN_FIELDS
    assert _version(account) == version


def test_a_second_real_revocation_after_a_reconnect_flips_it_again(
    account: str, tk_env: TokenKeeperEnv
) -> None:
    assert run_github_webhook(github_delivery()) == 204
    connect_and_callback()
    assert account_item(account)["state"] == {"S": "active"}
    new = github_delivery(headers={"X-GitHub-Delivery": "0d6f4b80-0000-11ef-8000-0000000000aa"})
    assert run_github_webhook(new) == 204
    assert account_item(account)["state_reason"] == {"S": "revoked"}


def test_a_revocation_replaces_another_needs_reauth_reason(account: str) -> None:
    assert Store(TK_TABLE).mark_needs_reauth_takeover(account, reason="refresh_rejected")
    before = _version(account)
    assert run_github_webhook(github_delivery()) == 204
    assert account_item(account)["state_reason"] == {"S": "revoked"}
    assert _version(account) == before + 1


def test_a_base64_encoded_body_is_verified_over_its_bytes(account: str) -> None:
    event = github_delivery()
    event["body"] = base64.b64encode(event["body"].encode()).decode()
    event["isBase64Encoded"] = True
    assert run_github_webhook(event) == 204
    assert account_item(account)["state"] == {"S": "needs_reauth"}


# Rejected: nothing parsed, written or sent -----------------------------------------


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(lambda: github_delivery(secret="not-the-secret-" + "x" * 30), id="bad"),
        pytest.param(lambda: github_delivery(headers={"X-Hub-Signature-256": None}), id="missing"),
        pytest.param(
            lambda: github_delivery(headers={"X-Hub-Signature-256": "sha256=" + "0" * 64}),
            id="zeros",
        ),
    ],
)
def test_unsigned_deliveries_get_401(
    account: str, tk_env: TokenKeeperEnv, event: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    version = _version(account)
    assert run_github_webhook(event()) == 401
    _assert_untouched(account, version, tk_env)
    assert '"github_webhook.rejected"' in capsys.readouterr().out


def test_the_signature_is_checked_before_the_body_is_parsed(
    account: str, tk_env: TokenKeeperEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    event = github_delivery(secret="not-the-secret-" + "x" * 30, body=b"{not json")
    assert run_github_webhook(event) == 401  # not 400: the JSON was never read
    assert '"bad_signature"' in capsys.readouterr().out


def test_a_signed_body_that_isnt_json_gets_400(account: str, tk_env: TokenKeeperEnv) -> None:
    version = _version(account)
    assert run_github_webhook(github_delivery(body=b"{not json")) == 400
    _assert_untouched(account, version, tk_env)


@pytest.mark.parametrize("sender_id", ["1234", True, None, 12.5])
def test_a_signed_revocation_without_an_integer_sender_id_gets_400(
    account: str, tk_env: TokenKeeperEnv, sender_id: object
) -> None:
    version = _version(account)
    body = json.dumps({"action": "revoked", "sender": {"id": sender_id}}).encode()
    assert run_github_webhook(github_delivery(body=body)) == 400
    _assert_untouched(account, version, tk_env)


# Ignored: 204 and a log line ------------------------------------------------------


def test_an_unknown_user_gets_204_and_nothing_changes(
    account: str, tk_env: TokenKeeperEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    version = _version(account)
    stranger = dict(VECTORS["github_app_authorization"]["body"])
    stranger["sender"] = {**stranger["sender"], "id": 999999}
    assert run_github_webhook(github_delivery(body=json.dumps(stranger).encode())) == 204
    _assert_untouched(account, version, tk_env)
    assert '"unknown_user"' in capsys.readouterr().out


def test_another_event_type_gets_204(
    account: str, tk_env: TokenKeeperEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    version = _version(account)
    assert run_github_webhook(github_delivery("ping")) == 204
    _assert_untouched(account, version, tk_env)
    assert '"ignored_event"' in capsys.readouterr().out


def test_a_ping_body_relabelled_as_a_revocation_is_ignored(
    account: str, tk_env: TokenKeeperEnv
) -> None:
    """The event name is an unsigned header; the signed body must say "revoked" too."""
    version = _version(account)
    ping = github_delivery("ping", headers={"X-GitHub-Event": "github_app_authorization"})
    assert run_github_webhook(ping) == 204
    _assert_untouched(account, version, tk_env)


def test_a_revocation_body_under_another_event_name_is_ignored(
    account: str, tk_env: TokenKeeperEnv
) -> None:
    version = _version(account)
    assert run_github_webhook(github_delivery(headers={"X-GitHub-Event": "ping"})) == 204
    _assert_untouched(account, version, tk_env)


def test_nothing_from_the_body_is_logged(account: str, capsys: pytest.CaptureFixture[str]) -> None:
    run_github_webhook(github_delivery())
    out = capsys.readouterr().out
    assert "octo-demo" not in out and "avatars" not in out


def test_no_table_rows_appear_for_ignored_deliveries(tk_env: TokenKeeperEnv) -> None:
    assert run_github_webhook(github_delivery()) == 204  # no account at all
    assert scan_table(TK_TABLE) == []
    assert message_bodies(tk_env.queue_url) == []


def test_user_body_fixture_matches_the_delivery_sender() -> None:
    # The recorded delivery's sender is the same synthetic user the GitHub fakes connect.
    assert VECTORS["github_app_authorization"]["body"]["sender"]["id"] == user_body()["id"]
