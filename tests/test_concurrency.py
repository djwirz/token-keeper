"""The three concurrency tests in spec §10, against moto DynamoDB.

Each one interleaves two writers at an exact point: the second runs inside the first's
mocked GitHub call. Real threads would race somewhere random; this pins the moment
the spec worries about and makes the test deterministic.
"""

from __future__ import annotations

import time
import typing
from collections.abc import Iterator
from dataclasses import fields
from urllib.parse import parse_qs

import boto3
import httpx
import pytest
import respx

from tests.conftest import TK_TABLE, TokenKeeperEnv
from tests.github_fakes import (
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
)
from token_keeper import crypto
from token_keeper.handlers import _shared
from token_keeper.store import Account, EncryptedTokens, LeasedAccount, Store

THIRD_ACCESS = "ghu_" + "FakeAccessToken3333NotReal3333333333"
THIRD_REFRESH = "ghr_" + "FakeRefreshToken333NotReal" + "3" * 50


@pytest.fixture
def gh() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def setup(gh: respx.MockRouter, tk_env: TokenKeeperEnv) -> tuple[str, respx.Route, respx.Route]:
    """A connected account whose access token is near expiry, so a sync must refresh."""
    token, user = mock_github(gh)
    mock_events(gh)
    connect_and_callback()
    account_id = Store(TK_TABLE).account_for_user("demo", "u_demo")
    assert account_id
    purge(tk_env.sync_queue_url)
    purge(tk_env.queue_url)
    token.reset()
    user.reset()
    set_numbers(account_id, access_expires_at=int(time.time()) + 300)
    return account_id, token, user


def test_two_workers_race_and_only_one_refreshes(
    setup: tuple[str, respx.Route, respx.Route], capsys: pytest.CaptureFixture[str]
) -> None:
    account_id, token, _ = setup
    b_result: list[object] = []

    def a_refreshes(request: httpx.Request) -> httpx.Response:
        # Worker B arrives while A holds the lease and is mid-refresh.
        b_result.append(run_worker(account_id))
        return httpx.Response(200, json=token_body(ROTATED_ACCESS_TOKEN, ROTATED_REFRESH_TOKEN))

    token.side_effect = a_refreshes
    assert run_worker(account_id) == {"batchItemFailures": []}

    # Exactly one refresh call: A's. B ran inside it and made none of its own.
    assert token.call_count == 1
    # B dropped its message cleanly (no retry, no DLQ).
    assert b_result == [{"batchItemFailures": []}]
    assert '"sync.skipped"' in capsys.readouterr().out
    # The stored pair is exactly what that one call returned.
    assert stored_tokens(account_id) == (ROTATED_ACCESS_TOKEN, ROTATED_REFRESH_TOKEN)
    assert account_item(account_id)["state"] == {"S": "active"}


def test_stale_read_still_refreshes_with_the_current_token(
    setup: tuple[str, respx.Route, respx.Route],
) -> None:
    account_id, token, _ = setup
    # Worker B read the account before A's refresh: it holds the original pair.
    stale = boto3.client("dynamodb").get_item(
        TableName=TK_TABLE,
        Key={"PK": {"S": f"ACCOUNT#{account_id}"}, "SK": {"S": "META"}},
        ConsistentRead=True,
    )["Item"]
    stale_refresh = _shared.crypto().decrypt(
        bytes(stale["refresh_token_ct"]["B"]), crypto.account_context(account_id)
    )
    assert stale_refresh == REFRESH_TOKEN

    # A refreshes and finishes. GitHub gives a short-lived access token this time, so
    # B will need to refresh too.
    short = token_body(ROTATED_ACCESS_TOKEN, ROTATED_REFRESH_TOKEN) | {"expires_in": 300}
    token.mock(return_value=httpx.Response(200, json=short))
    run_worker(account_id)
    assert stored_tokens(account_id) == (ROTATED_ACCESS_TOKEN, ROTATED_REFRESH_TOKEN)

    # Now B takes the lease. It must refresh with A's rotated token, not what it read.
    token.mock(return_value=httpx.Response(200, json=token_body(THIRD_ACCESS, THIRD_REFRESH)))
    run_worker(account_id)
    assert token.call_count == 2
    sent = [parse_qs(call.request.content.decode())["refresh_token"] for call in token.calls]
    assert sent == [[REFRESH_TOKEN], [ROTATED_REFRESH_TOKEN]]
    assert stored_tokens(account_id) == (THIRD_ACCESS, THIRD_REFRESH)


def test_only_the_lease_hands_out_token_ciphertext() -> None:
    """The structural half of the stale-read guarantee: nothing else can return tokens."""
    token_types = {EncryptedTokens, LeasedAccount}
    returning = {
        name
        for name, method in vars(Store).items()
        if callable(method)
        and not name.startswith("_")
        and token_types & set(typing.get_args(typing.get_type_hints(method).get("return")))
        | ({typing.get_type_hints(method).get("return")} & token_types)
    }
    assert returning == {"take_lease"}
    assert not {f.name for f in fields(Account)} & {"access_token_ct", "refresh_token_ct"}


def _assert_revoked_with_no_tokens(account_id: str) -> None:
    item = account_item(account_id)
    assert item["state"] == {"S": "needs_reauth"}
    assert item["state_reason"] == {"S": "revoked"}
    assert not {
        "access_token_ct",
        "refresh_token_ct",
        "next_due_at",
        "lease_owner",
        "lease_until",
    } & set(item)


def test_takeover_mid_sync_wins(
    setup: tuple[str, respx.Route, respx.Route],
    tk_env: TokenKeeperEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    account_id, _, user = setup
    set_numbers(account_id, access_expires_at=int(time.time()) + 3600)  # no refresh

    def revoked_during_sync(request: httpx.Request) -> httpx.Response:
        # The revocation webhook's takeover write (slice 5) lands while A is syncing.
        assert Store(TK_TABLE).mark_needs_reauth_takeover(account_id, reason="revoked")
        return httpx.Response(200, json=user_body())

    user.side_effect = revoked_during_sync
    assert run_worker(account_id) == {"batchItemFailures": []}

    assert '"lease_lost"' in capsys.readouterr().out
    _assert_revoked_with_no_tokens(account_id)
    # A's snapshot was discarded with its final write: still the connect-time one.
    assert snapshot_of(account_id)["recent_events"] == []
    # A sent no event; the takeover writer sends its own (slice 5).
    assert message_bodies(tk_env.queue_url) == []


def test_takeover_mid_refresh_discards_the_rotated_pair(
    setup: tuple[str, respx.Route, respx.Route], capsys: pytest.CaptureFixture[str]
) -> None:
    account_id, token, user = setup

    def revoked_during_refresh(request: httpx.Request) -> httpx.Response:
        assert Store(TK_TABLE).mark_needs_reauth_takeover(account_id, reason="revoked")
        return httpx.Response(200, json=token_body(ROTATED_ACCESS_TOKEN, ROTATED_REFRESH_TOKEN))

    token.side_effect = revoked_during_refresh
    run_worker(account_id)

    assert '"lease_lost"' in capsys.readouterr().out
    # The pair from the replaced grant was never written, and never used.
    _assert_revoked_with_no_tokens(account_id)
    assert user.call_count == 0
