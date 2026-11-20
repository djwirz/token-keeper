"""EMF metrics (spec §8, ADR 0019): each path counts exactly its metric, one dimension.

The handlers print EMF to stdout, which CloudWatch Logs turns into metrics. These tests
read that same stdout, so they check what CloudWatch would receive.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import respx

from tests.conftest import RECEIVER_WEBHOOK_URL, TK_TABLE, TokenKeeperEnv, http_event
from tests.github_fakes import mock_events, mock_github
from tests.tk_flow import (
    connect_and_callback,
    connect_event,
    drain,
    github_delivery,
    purge,
    run_callback,
    run_github_webhook,
    run_worker,
    set_numbers,
    signed_query,
    sync_record,
)
from token_keeper import metrics
from token_keeper.handlers import callback, connect, deliverer, planner, worker
from token_keeper.log import SERVICE
from token_keeper.store import Store


@dataclass
class Emitted:
    counts: Counter[str]
    values: dict[str, list[float]]
    blobs: list[dict[str, Any]]


def emitted(capsys: pytest.CaptureFixture[str]) -> Emitted:
    """Every EMF line printed since the last read, checked for the one-dimension rule."""
    counts: Counter[str] = Counter()
    values: dict[str, list[float]] = {}
    blobs = []
    for line in capsys.readouterr().out.splitlines():
        try:
            blob = json.loads(line)
        except ValueError:
            continue
        if not isinstance(blob, dict) or "_aws" not in blob:
            continue
        blobs.append(blob)
        for directive in blob["_aws"]["CloudWatchMetrics"]:
            assert directive["Namespace"] == metrics.NAMESPACE
            assert directive["Dimensions"] == [["service"]]
            assert blob["service"] == SERVICE
            for metric in directive["Metrics"]:
                name = metric["Name"]
                assert name in metrics.ALL, name  # never ColdStart or a stray name
                raw = blob[name]
                vals = raw if isinstance(raw, list) else [raw]
                values.setdefault(name, []).extend(float(v) for v in vals)
                counts[name] += int(sum(vals)) if metric["Unit"] == "Count" else len(vals)
    return Emitted(counts, values, blobs)


@pytest.fixture
def gh() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def token_route(gh: respx.MockRouter) -> respx.Route:
    route, _ = mock_github(gh)
    mock_events(gh)
    return route


@pytest.fixture
def acc(
    token_route: respx.Route, tk_env: TokenKeeperEnv, capsys: pytest.CaptureFixture[str]
) -> str:
    """A connected account with its queues cleared and stdout read."""
    connect_and_callback()
    account_id = Store(TK_TABLE).account_for_user("demo", "u_demo")
    assert account_id
    purge(tk_env.sync_queue_url)
    purge(tk_env.queue_url)
    capsys.readouterr()
    return account_id


def _near_expiry(account_id: str) -> None:
    set_numbers(account_id, access_expires_at=int(time.time()) + 300)


# Worker -------------------------------------------------------------------------


def test_a_plain_sync_counts_one_success(acc: str, capsys: pytest.CaptureFixture[str]) -> None:
    run_worker(acc)
    assert emitted(capsys).counts == Counter({metrics.SYNC_SUCCEEDED: 1})


def test_a_refresh_then_sync_counts_both(acc: str, capsys: pytest.CaptureFixture[str]) -> None:
    _near_expiry(acc)
    run_worker(acc)
    assert emitted(capsys).counts == Counter(
        {metrics.REFRESH_SUCCEEDED: 1, metrics.SYNC_SUCCEEDED: 1}
    )


def test_a_rejected_refresh_is_refresh_failed(
    acc: str, token_route: respx.Route, capsys: pytest.CaptureFixture[str]
) -> None:
    _near_expiry(acc)
    token_route.mock(return_value=httpx.Response(200, json={"error": "bad_refresh_token"}))
    run_worker(acc)
    assert emitted(capsys).counts == Counter({metrics.REFRESH_FAILED: 1, metrics.SYNC_FAILED: 1})


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(502, json={}),
        httpx.Response(200, json={"error": "unexpected_thing"}),
        httpx.ConnectTimeout("slow"),
    ],
    ids=["5xx", "other-error", "timeout"],
)
def test_a_transient_refresh_error_is_not_refresh_failed(
    acc: str,
    token_route: respx.Route,
    capsys: pytest.CaptureFixture[str],
    response: httpx.Response | Exception,
) -> None:
    # A GitHub outage mustn't page anyone (ADR 0015, ADR 0019).
    _near_expiry(acc)
    route = token_route
    if isinstance(response, Exception):
        route.mock(side_effect=response)
    else:
        route.mock(return_value=response)
    run_worker(acc)
    assert emitted(capsys).counts == Counter({metrics.SYNC_FAILED: 1})


def test_an_expired_refresh_token_is_a_failed_sync_without_a_refresh(
    acc: str, capsys: pytest.CaptureFixture[str]
) -> None:
    set_numbers(acc, refresh_expires_at=int(time.time()) - 1)
    run_worker(acc)
    assert emitted(capsys).counts == Counter({metrics.SYNC_FAILED: 1})


def test_a_skipped_sync_counts_nothing_and_prints_nothing(
    acc: str, capsys: pytest.CaptureFixture[str]
) -> None:
    Store(TK_TABLE).mark_needs_reauth_takeover(acc, reason="revoked")
    run_worker(acc)
    found = emitted(capsys)
    assert found.counts == Counter()
    assert found.blobs == []


def test_a_poison_message_is_a_failed_sync(acc: str, capsys: pytest.CaptureFixture[str]) -> None:
    # The DLQ drill (ADR 0019): a body that isn't JSON fails the record.
    poison = sync_record(acc, "m-poison") | {"body": "alarm drill: not json"}
    result = worker.handler({"Records": [poison]}, None)
    assert result == {"batchItemFailures": [{"itemIdentifier": "m-poison"}]}
    assert emitted(capsys).counts == Counter({metrics.SYNC_FAILED: 1})


def test_one_invocation_prints_one_emf_line(acc: str, capsys: pytest.CaptureFixture[str]) -> None:
    _near_expiry(acc)
    run_worker(acc)
    assert len(emitted(capsys).blobs) == 1


# Planner ------------------------------------------------------------------------


def test_the_planner_counts_due_accounts_even_when_none_are(
    acc: str, capsys: pytest.CaptureFixture[str]
) -> None:
    set_numbers(acc, next_due_at=int(time.time()) + 1000)
    planner.handler({}, None)
    found = emitted(capsys)
    # Emitted at zero: the one metric that's there around the clock.
    assert found.values[metrics.ACCOUNTS_DUE] == [0.0]

    set_numbers(acc, next_due_at=int(time.time()) - 10)
    planner.handler({}, None)
    assert emitted(capsys).values[metrics.ACCOUNTS_DUE] == [1.0]


# Deliverer ----------------------------------------------------------------------


def test_a_delivery_counts_success_and_latency_from_sent_timestamp(
    acc: str,
    gh: respx.MockRouter,
    tk_env: TokenKeeperEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gh.post(RECEIVER_WEBHOOK_URL).mock(return_value=httpx.Response(204))
    sent = int(time.time() * 1000) - 4000
    record = {
        "messageId": "m-1",
        "receiptHandle": "rh",
        "body": json.dumps({"account_id": acc, "type": "account.updated", "webhook_id": "msg_x"}),
        "attributes": {"ApproximateReceiveCount": "3", "SentTimestamp": str(sent)},
        "messageAttributes": {},
    }
    assert deliverer.handler({"Records": [record]}, None) == {"batchItemFailures": []}
    found = emitted(capsys)
    assert found.counts[metrics.DELIVERY_SUCCEEDED] == 1
    assert metrics.DELIVERY_FAILED not in found.counts
    [latency] = found.values[metrics.DELIVERY_LATENCY]
    # From the first send, so retries count: about 4 s, not the POST's few ms.
    assert 4000 <= latency < 10000


def test_the_local_runner_passes_sent_timestamp_like_lambda(
    gh: respx.MockRouter, tk_env: TokenKeeperEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_github(gh)
    gh.post(RECEIVER_WEBHOOK_URL).mock(return_value=httpx.Response(204))
    connect_and_callback()
    capsys.readouterr()
    assert drain(tk_env.queue_url) == 1
    assert len(emitted(capsys).values[metrics.DELIVERY_LATENCY]) == 1


@pytest.mark.parametrize(
    "response",
    [httpx.Response(500), httpx.ConnectTimeout("slow")],
    ids=["500", "timeout"],
)
def test_a_failed_delivery_counts_once_and_no_latency(
    acc: str,
    gh: respx.MockRouter,
    tk_env: TokenKeeperEnv,
    capsys: pytest.CaptureFixture[str],
    response: httpx.Response | Exception,
) -> None:
    route = gh.post(RECEIVER_WEBHOOK_URL)
    if isinstance(response, Exception):
        route.mock(side_effect=response)
    else:
        route.mock(return_value=response)
    run_worker(acc)  # enqueues account.updated
    capsys.readouterr()
    assert drain(tk_env.queue_url) == 1
    assert emitted(capsys).counts == Counter({metrics.DELIVERY_FAILED: 1})


def test_a_delivery_that_raises_counts_as_failed(
    tk_env: TokenKeeperEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    record = {
        "messageId": "m-bad",
        "receiptHandle": "rh",
        "body": "not json",
        "attributes": {"ApproximateReceiveCount": "1"},
    }
    deliverer.handler({"Records": [record]}, None)
    assert emitted(capsys).counts == Counter({metrics.DELIVERY_FAILED: 1})


# Connect and callback -------------------------------------------------------------


def test_a_rejected_connect_link_is_connect_rejected(
    tk_env: TokenKeeperEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    connect.handler(connect_event(signed_query() + "0"), None)
    assert emitted(capsys).counts == Counter({metrics.CONNECT_REJECTED: 1})


def test_a_good_connect_counts_nothing(
    tk_env: TokenKeeperEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    connect.handler(connect_event(signed_query()), None)
    assert emitted(capsys).counts == Counter()


@pytest.mark.parametrize(
    "query",
    [{}, {"state": "unknown", "code": "c"}],
    ids=["missing-state", "unknown-state"],
)
def test_a_rejected_callback_is_connect_rejected(
    tk_env: TokenKeeperEnv, capsys: pytest.CaptureFixture[str], query: dict[str, str]
) -> None:
    callback.handler(http_event("GET", "/callback", query=query), None)
    assert emitted(capsys).counts == Counter({metrics.CONNECT_REJECTED: 1})


def test_a_successful_callback_counts_nothing(
    token_route: respx.Route, tk_env: TokenKeeperEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    connect_and_callback()
    assert emitted(capsys).counts == Counter()


def test_a_callback_missing_its_code_is_connect_rejected(
    tk_env: TokenKeeperEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.tk_flow import start_connect

    _, params = start_connect()
    capsys.readouterr()
    run_callback({"state": params["state"]})
    assert emitted(capsys).counts == Counter({metrics.CONNECT_REJECTED: 1})


# GitHub webhook -------------------------------------------------------------------


def test_a_revocation_is_received(acc: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_github_webhook(github_delivery()) == 204
    # And a redelivery is received again, though it writes nothing.
    assert run_github_webhook(github_delivery()) == 204
    assert emitted(capsys).counts == Counter({metrics.REVOCATION_RECEIVED: 2})


def test_a_forged_delivery_is_github_webhook_rejected(
    acc: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_github_webhook(github_delivery(secret="not-the-secret")) == 401
    assert run_github_webhook(github_delivery(headers={"X-Hub-Signature-256": None})) == 401
    assert run_github_webhook(github_delivery(body=b"[not json")) == 400
    assert emitted(capsys).counts == Counter({metrics.GITHUB_WEBHOOK_REJECTED: 3})


def test_an_ignored_event_counts_nothing(acc: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_github_webhook(github_delivery("ping")) == 204
    assert emitted(capsys).counts == Counter()


def test_metric_names_are_unique_and_include_every_count() -> None:
    assert len(set(metrics.ALL)) == len(metrics.ALL) == 11
