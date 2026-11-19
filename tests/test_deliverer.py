"""The deliverer: current state → signed Standard Webhooks POST (spec §6.3)."""

from __future__ import annotations

import json
from collections.abc import Iterator

import boto3
import httpx
import pytest
import respx

from receiver.handler import handler as receiver_handler
from tests.conftest import RECEIVER_WEBHOOK_URL, WEBHOOK_SECRET, TokenKeeperEnv, http_event
from tests.github_fakes import mock_github
from tests.tk_flow import connect_and_callback, drain, queue_messages
from token_keeper.contract import webhooks
from token_keeper.contract.events import parse_event
from token_keeper.handlers import deliverer

pytestmark = pytest.mark.usefixtures("tk_env")


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mock:
        mock_github(mock)
        yield mock


def _to_receiver(request: httpx.Request) -> httpx.Response:
    """Hand the POST to the real receiver handler, as API Gateway would."""
    event = http_event(
        "POST",
        "/webhooks/token-keeper",
        body=request.content.decode(),
        headers=dict(request.headers),
    )
    response = receiver_handler(event, None)
    return httpx.Response(int(response["statusCode"]))


def test_connected_event_reaches_the_receiver(
    router: respx.MockRouter, tk_env: TokenKeeperEnv
) -> None:
    route = router.post(RECEIVER_WEBHOOK_URL).mock(side_effect=_to_receiver)
    connect_and_callback()
    assert drain(tk_env.queue_url) == 1

    request = route.calls.last.request
    webhook_id = webhooks.verify(WEBHOOK_SECRET, dict(request.headers), request.content)
    assert webhook_id.startswith("msg_")
    event = parse_event(json.loads(request.content))
    assert event.type == "account.connected"
    assert event.version == 1
    assert event.state == "active" and event.state_reason is None
    assert event.github.login == "octo-demo"
    assert event.snapshot is not None and event.snapshot.profile.name == "Octo Demo"
    assert event.snapshot.recent_events == []
    # Nothing token-shaped or expiry-related in the payload (spec §6.3).
    body = request.content.decode()
    for forbidden in ("ghu_", "ghr_", "token", "expires"):
        assert forbidden not in body

    assert queue_messages(tk_env.queue_url) == []  # delivered, so deleted
    page = receiver_handler(http_event("GET", "/"), None)["body"]
    assert "@octo-demo" in page


def test_body_is_built_from_state_at_send_time(
    router: respx.MockRouter, tk_env: TokenKeeperEnv
) -> None:
    route = router.post(RECEIVER_WEBHOOK_URL).mock(return_value=httpx.Response(204))
    connect_and_callback()
    connect_and_callback()  # reconnect: version 2, a second message
    assert drain(tk_env.queue_url) == 2
    versions = {json.loads(c.request.content)["version"] for c in route.calls}
    assert versions == {2}  # the first message, sent late, carries the current state


@pytest.mark.parametrize(
    "response",
    [httpx.Response(500), httpx.Response(401), httpx.ConnectTimeout("slow")],
    ids=["500", "401", "timeout"],
)
def test_failure_is_retried_with_backoff(
    router: respx.MockRouter,
    tk_env: TokenKeeperEnv,
    response: httpx.Response | Exception,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if isinstance(response, Exception):
        router.post(RECEIVER_WEBHOOK_URL).mock(side_effect=response)
    else:
        router.post(RECEIVER_WEBHOOK_URL).mock(return_value=response)
    connect_and_callback()

    visibility: list[int] = []
    real = deliverer._sqs()
    original = real.change_message_visibility

    def spy(**kwargs: object) -> object:
        visibility.append(int(kwargs["VisibilityTimeout"]))  # type: ignore[call-overload]
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(real, "change_message_visibility", spy)
    assert drain(tk_env.queue_url) == 1
    assert visibility == [deliverer.FIRST_BACKOFF_SECONDS]
    # Not deleted: it comes back after the backoff (and goes to the DLQ in the end).
    sqs = boto3.client("sqs")
    attrs = sqs.get_queue_attributes(
        QueueUrl=tk_env.queue_url, AttributeNames=["ApproximateNumberOfMessagesNotVisible"]
    )["Attributes"]
    assert attrs["ApproximateNumberOfMessagesNotVisible"] == "1"


def test_one_bad_record_does_not_fail_the_batch(
    router: respx.MockRouter, tk_env: TokenKeeperEnv
) -> None:
    router.post(RECEIVER_WEBHOOK_URL).mock(return_value=httpx.Response(204))
    connect_and_callback()
    (good,) = boto3.client("sqs").receive_message(
        QueueUrl=tk_env.queue_url, AttributeNames=["All"], MessageAttributeNames=["All"]
    )["Messages"]
    from local.queues import to_record

    bad = {**to_record(good), "messageId": "bad", "body": "not json"}
    result = deliverer.handler({"Records": [to_record(good), bad]}, None)
    assert result == {"batchItemFailures": [{"itemIdentifier": "bad"}]}


def test_missing_account_is_dropped(router: respx.MockRouter) -> None:
    route = router.post(RECEIVER_WEBHOOK_URL).mock(return_value=httpx.Response(204))
    record = {
        "messageId": "m1",
        "receiptHandle": "r1",
        "body": json.dumps(
            {"account_id": "acc_gone", "type": "account.updated", "webhook_id": "msg_1"}
        ),
        "attributes": {"ApproximateReceiveCount": "1"},
    }
    assert deliverer.handler({"Records": [record]}, None) == {"batchItemFailures": []}
    assert not route.called


def test_backoff_doubles_to_a_cap_and_lasts_about_a_day() -> None:
    delays = [deliverer.backoff_seconds(n) for n in range(1, deliverer.MAX_RECEIVES)]
    assert delays[:3] == [30, 60, 120]
    assert max(delays) == deliverer.MAX_BACKOFF_SECONDS
    total_hours = sum(delays) / 3600
    assert 20 <= total_hours <= 30
