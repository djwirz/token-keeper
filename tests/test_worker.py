"""The worker: lease, refresh, sync and the state machine (spec §4, §7)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from tests.conftest import TK_TABLE, TokenKeeperEnv
from tests.github_fakes import (
    ACCESS_TOKEN,
    REFRESH_TOKEN,
    ROTATED_ACCESS_TOKEN,
    ROTATED_REFRESH_TOKEN,
    mock_events,
    mock_github,
    token_body,
    user_body,
)
from tests.tk_flow import (
    account_item,
    connect_and_callback,
    message_bodies,
    purge,
    run_worker,
    set_numbers,
    snapshot_of,
    stored_tokens,
    sync_record,
)
from token_keeper import sync
from token_keeper.contract.events import parse_event
from token_keeper.handlers import deliverer, worker
from token_keeper.store import Account, EncryptedTokens, LeasedAccount, LeaseLost, Store

TOKEN_FIELDS = {"access_token_ct", "refresh_token_ct", "access_expires_at", "refresh_expires_at"}


@dataclass
class Connected:
    account_id: str
    token: respx.Route
    user: respx.Route
    events: respx.Route


@pytest.fixture
def gh() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def acc(gh: respx.MockRouter, tk_env: TokenKeeperEnv) -> Connected:
    """A connected account, with the callback's messages cleared and call counts reset."""
    token, user = mock_github(gh)
    events = mock_events(gh)
    connect_and_callback()
    account_id = Store(TK_TABLE).account_for_user("demo", "u_demo")
    assert account_id
    purge(tk_env.sync_queue_url)
    purge(tk_env.queue_url)
    for route in (token, user, events):
        route.reset()
    return Connected(account_id, token, user, events)


def _rotate(acc: Connected, expires_in: int = 28800) -> None:
    body = token_body(ROTATED_ACCESS_TOKEN, ROTATED_REFRESH_TOKEN) | {"expires_in": expires_in}
    acc.token.mock(return_value=httpx.Response(200, json=body))


def _near_expiry(acc: Connected) -> None:
    set_numbers(acc.account_id, access_expires_at=int(time.time()) + 300)


def _sent(route: respx.Route, n: int = -1) -> dict[str, list[str]]:
    return parse_qs(route.calls[n].request.content.decode())


def _events(tk_env: TokenKeeperEnv) -> list[str]:
    return [m["type"] for m in message_bodies(tk_env.queue_url)]


def _assert_needs_reauth(account_id: str, reason: str) -> None:
    item = account_item(account_id)
    assert item["state"] == {"S": "needs_reauth"}
    assert item["state_reason"] == {"S": reason}
    # Tokens deleted, out of the due index, lease cleared (spec §4, §7).
    assert not TOKEN_FIELDS & set(item)
    assert not {"next_due_at", "lease_owner", "lease_until"} & set(item)
    # The public snapshot stays, so the card still shows who must reconnect (spec §9).
    assert snapshot_of(account_id)["profile"]["name"] == "Octo Demo"


# The callback's immediate sync ------------------------------------------------


def test_callback_enqueues_an_immediate_sync(gh: respx.MockRouter, tk_env: TokenKeeperEnv) -> None:
    mock_github(gh)
    connect_and_callback()
    account_id = Store(TK_TABLE).account_for_user("demo", "u_demo")
    # Ids only, never a token (spec §9).
    assert message_bodies(tk_env.sync_queue_url) == [{"account_id": account_id}]
    assert _events(tk_env) == ["account.connected"]
    # The planner is only the fallback: due in about 30 minutes, not now.
    due = int(account_item(str(account_id))["next_due_at"]["N"])
    assert due - time.time() == pytest.approx(sync.PLANNER_PUSH_SECONDS, abs=5)


# Success ----------------------------------------------------------------------


def test_sync_with_a_valid_token_makes_no_token_call(
    acc: Connected, tk_env: TokenKeeperEnv
) -> None:
    before = int(account_item(acc.account_id)["version"]["N"])
    assert run_worker(acc.account_id) == {"batchItemFailures": []}

    assert acc.token.call_count == 0
    assert acc.user.calls.last.request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert acc.events.calls.last.request.url.params["per_page"] == "10"
    snapshot = snapshot_of(acc.account_id)
    assert len(snapshot["recent_events"]) == 10
    # Newest first, whatever order GitHub listed them in.
    times = [e["created_at"] for e in snapshot["recent_events"]]
    assert times == sorted(times, reverse=True)
    assert snapshot["recent_events"][0] == {
        "type": "PushEvent",
        "repo": "octo-demo/repo-9",
        "created_at": "2026-11-18T19:00:00Z",
    }
    item = account_item(acc.account_id)
    assert item["state"] == {"S": "active"}
    # Lease taken and released: two version bumps.
    assert int(item["version"]["N"]) == before + 2
    assert not {"lease_owner", "lease_until", "last_error_code"} & set(item)
    due = int(item["next_due_at"]["N"]) - time.time()
    assert due >= sync.SYNC_INTERVAL_SECONDS - sync.JITTER_SECONDS - 5
    assert due <= sync.SYNC_INTERVAL_SECONDS + sync.JITTER_SECONDS + 5
    assert _events(tk_env) == ["account.updated"]


def test_the_event_carries_the_synced_snapshot(acc: Connected) -> None:
    run_worker(acc.account_id)
    found = Store(TK_TABLE).get_account_and_snapshot(acc.account_id)
    assert found
    event = deliverer.build_event("account.updated", *found, int(time.time()))
    parsed = parse_event(event.to_dict())
    assert parsed.snapshot is not None
    assert len(parsed.snapshot.recent_events) == 10
    assert parsed.state == "active"


def test_near_expiry_refreshes_first_and_stores_the_new_pair(acc: Connected) -> None:
    _near_expiry(acc)
    _rotate(acc)
    run_worker(acc.account_id)

    assert acc.token.call_count == 1
    sent = _sent(acc.token)
    assert sent["grant_type"] == ["refresh_token"]
    assert sent["refresh_token"] == [REFRESH_TOKEN]
    assert acc.token.calls.last.request.headers["accept"] == "application/json"
    # The sync used the new access token, and the new pair is what's stored.
    assert acc.user.calls.last.request.headers["authorization"] == (
        f"Bearer {ROTATED_ACCESS_TOKEN}"
    )
    assert stored_tokens(acc.account_id) == (ROTATED_ACCESS_TOKEN, ROTATED_REFRESH_TOKEN)
    item = account_item(acc.account_id)
    assert int(item["access_expires_at"]["N"]) - time.time() == pytest.approx(28800, abs=5)


def test_sync_uses_the_login_from_user(acc: Connected, gh: respx.MockRouter) -> None:
    acc.user.mock(return_value=httpx.Response(200, json=user_body(login="octo-renamed")))
    renamed = mock_events(gh, login="octo-renamed")
    run_worker(acc.account_id)
    assert renamed.call_count == 1
    assert account_item(acc.account_id)["login"] == {"S": "octo-renamed"}


# needs_reauth -----------------------------------------------------------------


def test_refresh_token_past_expiry_needs_reauth_without_calling_github(
    acc: Connected, tk_env: TokenKeeperEnv
) -> None:
    set_numbers(acc.account_id, refresh_expires_at=int(time.time()) - 1)
    run_worker(acc.account_id)
    assert acc.token.call_count == acc.user.call_count == acc.events.call_count == 0
    _assert_needs_reauth(acc.account_id, "refresh_expired")
    assert _events(tk_env) == ["account.updated"]


def test_bad_refresh_token_needs_reauth(acc: Connected, tk_env: TokenKeeperEnv) -> None:
    _near_expiry(acc)
    # GitHub answers token errors with a 200 and an error field (spec §5).
    acc.token.mock(return_value=httpx.Response(200, json={"error": "bad_refresh_token"}))
    run_worker(acc.account_id)
    assert acc.user.call_count == 0
    _assert_needs_reauth(acc.account_id, "refresh_rejected")
    assert _events(tk_env) == ["account.updated"]


def test_a_401_is_fixed_by_one_refresh(acc: Connected) -> None:
    acc.user.side_effect = [
        httpx.Response(401, json={}),
        httpx.Response(200, json=user_body()),
    ]
    _rotate(acc)
    run_worker(acc.account_id)
    assert acc.token.call_count == 1
    assert acc.user.calls[1].request.headers["authorization"] == f"Bearer {ROTATED_ACCESS_TOKEN}"
    assert account_item(acc.account_id)["state"] == {"S": "active"}
    assert stored_tokens(acc.account_id) == (ROTATED_ACCESS_TOKEN, ROTATED_REFRESH_TOKEN)


def test_a_401_after_the_refresh_is_unauthorized(acc: Connected, tk_env: TokenKeeperEnv) -> None:
    acc.user.mock(return_value=httpx.Response(401, json={}))
    _rotate(acc)
    run_worker(acc.account_id)
    assert acc.token.call_count == 1
    assert acc.user.call_count == 2
    _assert_needs_reauth(acc.account_id, "unauthorized")
    assert _events(tk_env) == ["account.updated"]


def test_a_401_on_the_events_call_also_refreshes_once(acc: Connected) -> None:
    acc.events.side_effect = [
        httpx.Response(401, json={}),
        httpx.Response(200, json=[]),
    ]
    _rotate(acc)
    run_worker(acc.account_id)
    assert acc.token.call_count == 1
    assert account_item(acc.account_id)["state"] == {"S": "active"}


def test_a_401_right_after_a_near_expiry_refresh_does_not_refresh_again(
    acc: Connected,
) -> None:
    _near_expiry(acc)
    _rotate(acc)
    acc.user.mock(return_value=httpx.Response(401, json={}))
    run_worker(acc.account_id)
    assert acc.token.call_count == 1
    _assert_needs_reauth(acc.account_id, "unauthorized")


def test_an_inactive_account_is_skipped(acc: Connected) -> None:
    assert Store(TK_TABLE).mark_needs_reauth_takeover(acc.account_id, reason="revoked")
    assert run_worker(acc.account_id) == {"batchItemFailures": []}
    assert acc.token.call_count == acc.user.call_count == 0


# Transient errors: stay active, back off, no event ------------------------------


def _assert_transient(acc: Connected, code: str, delay: float, failures: int) -> None:
    item = account_item(acc.account_id)
    assert item["state"] == {"S": "active"}
    assert item["last_error_code"] == {"S": code}
    assert item["consecutive_failures"] == {"N": str(failures)}
    assert int(item["next_due_at"]["N"]) - time.time() == pytest.approx(delay, abs=5)
    assert not {"lease_owner", "lease_until"} & set(item)
    assert "access_token_ct" in item


def test_rate_limit_waits_for_the_reset(acc: Connected, tk_env: TokenKeeperEnv) -> None:
    reset = int(time.time()) + 600
    acc.user.mock(
        return_value=httpx.Response(
            403,
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)},
            json={},
        )
    )
    run_worker(acc.account_id)
    _assert_transient(acc, "user_rate_limited", 600, 1)
    assert acc.token.call_count == 0
    assert _events(tk_env) == []


def test_retry_after_is_honoured_and_clamped(acc: Connected) -> None:
    acc.events.mock(return_value=httpx.Response(429, headers={"retry-after": "120"}, json={}))
    run_worker(acc.account_id)
    _assert_transient(acc, "events_rate_limited", 120, 1)

    acc.events.mock(return_value=httpx.Response(429, headers={"retry-after": "86400"}, json={}))
    run_worker(acc.account_id)
    _assert_transient(acc, "events_rate_limited", sync.MAX_BACKOFF_SECONDS, 2)


def test_a_403_without_rate_limit_headers_is_plain_transient(acc: Connected) -> None:
    acc.user.mock(return_value=httpx.Response(403, json={}))
    run_worker(acc.account_id)
    _assert_transient(acc, "user_http_403", sync.FIRST_BACKOFF_SECONDS, 1)


def test_5xx_backs_off_to_an_hour_then_success_resets(
    acc: Connected, tk_env: TokenKeeperEnv
) -> None:
    acc.user.mock(return_value=httpx.Response(502, json={}))
    for failures, delay in enumerate([900, 1800, 3600, 3600], start=1):
        run_worker(acc.account_id)
        _assert_transient(acc, "user_http_502", delay, failures)
    assert _events(tk_env) == []

    acc.user.mock(return_value=httpx.Response(200, json=user_body()))
    run_worker(acc.account_id)
    item = account_item(acc.account_id)
    assert item["consecutive_failures"] == {"N": "0"}
    assert "last_error_code" not in item
    assert _events(tk_env) == ["account.updated"]


def test_token_endpoint_timeout_is_transient_and_keeps_the_pair(acc: Connected) -> None:
    _near_expiry(acc)
    acc.token.mock(side_effect=httpx.ConnectTimeout("slow"))
    run_worker(acc.account_id)
    _assert_transient(acc, "token_unreachable", sync.FIRST_BACKOFF_SECONDS, 1)
    assert stored_tokens(acc.account_id) == (ACCESS_TOKEN, REFRESH_TOKEN)


def test_other_token_errors_are_transient(acc: Connected) -> None:
    _near_expiry(acc)
    acc.token.mock(return_value=httpx.Response(200, json={"error": "unexpected_thing"}))
    run_worker(acc.account_id)
    _assert_transient(acc, "token_error:unexpected_thing", sync.FIRST_BACKOFF_SECONDS, 1)


def test_no_token_call_starts_late_in_the_lease(
    acc: Connected, monkeypatch: pytest.MonkeyPatch
) -> None:
    _near_expiry(acc)
    start = int(time.time())
    # The lease is taken at `start`; every later clock read (run()'s, then the refresh
    # check's) is 45 s on, leaving 15 s of lease: under the 20 s guard.
    clock = iter([start, start + 45])
    monkeypatch.setattr(worker, "now", lambda: next(clock, start + 45))
    run_worker(acc.account_id)
    assert acc.token.call_count == 0
    item = account_item(acc.account_id)
    assert item["last_error_code"] == {"S": "lease_too_short"}
    assert stored_tokens(acc.account_id) == (ACCESS_TOKEN, REFRESH_TOKEN)


# The message ------------------------------------------------------------------


def test_an_unexpected_error_fails_only_that_record(acc: Connected) -> None:
    bad = sync_record(acc.account_id, "m-bad") | {"body": "not json"}
    result = worker.handler({"Records": [bad, sync_record(acc.account_id, "m-ok")]}, None)
    assert result == {"batchItemFailures": [{"itemIdentifier": "m-bad"}]}
    assert acc.user.call_count == 1


# finish_sync: only a failed lease condition is a lost lease (PR 4 review) ----------


class _CancellingClient:
    def __init__(self, first_reason: str) -> None:
        self.first_reason = first_reason

    def transact_write_items(self, **kwargs: object) -> None:
        from botocore.exceptions import ClientError

        raise ClientError(
            {
                "Error": {"Code": "TransactionCanceledException", "Message": "cancelled"},
                "CancellationReasons": [{"Code": self.first_reason}, {"Code": "None"}],
            },
            "TransactWriteItems",
        )


def _leased() -> LeasedAccount:
    account = Account("acc_X", "demo", "u_demo", 1234, "octo-demo", "active", None, 2)
    tokens = EncryptedTokens(b"a", b"r", 0, 0)
    return LeasedAccount(account, tokens, "wrk_X", 0, 0)


@pytest.mark.parametrize("reason", ["TransactionConflict", "ThrottlingError"])
def test_a_cancel_for_another_reason_is_not_a_lost_lease(reason: str) -> None:
    from botocore.exceptions import ClientError

    store = Store(TK_TABLE, client=_CancellingClient(reason))  # type: ignore[arg-type]
    with pytest.raises(ClientError):
        store.finish_sync(_leased(), login="octo-demo", snapshot={}, next_due_at=0)


def test_a_failed_lease_condition_is_a_lost_lease() -> None:
    store = Store(TK_TABLE, client=_CancellingClient("ConditionalCheckFailed"))  # type: ignore[arg-type]
    with pytest.raises(LeaseLost):
        store.finish_sync(_leased(), login="octo-demo", snapshot={}, next_due_at=0)
