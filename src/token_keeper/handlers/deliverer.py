"""Deliverer: SQS ``delivery-queue`` → signed POST to the main app (spec §6.3).

Each message names an account and an event type. The body is built from the account's
state *now*, not when the message was enqueued, so a late retry still sends the latest
full state; ``webhook-id`` comes from the message and stays the same across retries.

A 2xx is delivered. Anything else, or a timeout, fails that record only
(``batchItemFailures``) and pushes its next attempt out with exponential backoff.
After ``MAX_RECEIVES`` attempts (about a day) SQS moves it to the DLQ.
"""

from __future__ import annotations

import json
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
import httpx

from token_keeper import log, settings
from token_keeper.contract import webhooks
from token_keeper.contract.events import (
    AccountEvent,
    GitHubIdentity,
    Profile,
    RecentEvent,
    Snapshot,
)
from token_keeper.delivery import CORRELATION_ATTRIBUTE
from token_keeper.handlers._shared import now, store
from token_keeper.handlers.callback import iso
from token_keeper.store import Account

if TYPE_CHECKING:
    from mypy_boto3_sqs import SQSClient

# 5 s per phase, but only 2 s to connect: one worst-case POST stays well inside the
# Lambda's 15 s timeout (one message per invocation; see infra/stacks/token_keeper.py).
TIMEOUT = httpx.Timeout(5.0, connect=2.0)
FIRST_BACKOFF_SECONDS = 30
MAX_BACKOFF_SECONDS = 2 * 60 * 60
# 30 s doubling to a 2 h cap, then 2 h apart: attempt 20 lands about a day after the first.
MAX_RECEIVES = 20


def backoff_seconds(receive_count: int) -> int:
    return int(min(FIRST_BACKOFF_SECONDS * 2 ** max(receive_count - 1, 0), MAX_BACKOFF_SECONDS))


@cache
def _sqs() -> SQSClient:
    return boto3.client("sqs")


@cache
def _http() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, headers={"user-agent": "token-keeper"})


def build_event(
    event_type: str, account: Account, snapshot: dict[str, Any] | None, occurred_at: int
) -> AccountEvent:
    snap = None
    if snapshot is not None:
        prof = snapshot["profile"]
        snap = Snapshot(
            fetched_at=snapshot["fetched_at"],
            profile=Profile(
                name=prof.get("name"),
                avatar_url=prof.get("avatar_url"),
                public_repos=int(prof.get("public_repos", 0)),
                followers=int(prof.get("followers", 0)),
            ),
            recent_events=[RecentEvent(**e) for e in snapshot.get("recent_events", [])],
        )
    return AccountEvent(
        type=event_type,  # type: ignore[arg-type]
        app_id=account.app_id,
        account_id=account.account_id,
        external_user_id=account.external_user_id,
        version=account.version,
        state=account.state,  # type: ignore[arg-type]
        state_reason=account.state_reason,  # type: ignore[arg-type]
        provider="github",
        github=GitHubIdentity(user_id=account.github_user_id, login=account.login),
        snapshot=snap,
        occurred_at=iso(occurred_at),
    )


def deliver(record: dict[str, Any]) -> bool:
    """Send one message. True when it's done (delivered, or nothing left to send)."""
    logger = log.get_logger()
    message = json.loads(record["body"])
    account_id = message["account_id"]
    webhook_id = message["webhook_id"]
    attributes = record.get("messageAttributes") or {}
    correlation = (attributes.get(CORRELATION_ATTRIBUTE) or {}).get("stringValue")
    log.start(
        handler="deliverer",
        account_id=account_id,
        webhook_id=webhook_id,
        correlation_id=correlation,
    )

    found = store().get_account_and_snapshot(account_id)
    if found is None:
        # Deleted by an operator (runbook); nothing to report.
        logger.warning("delivery.account_missing")
        return True
    account, snapshot = found
    event = build_event(message["type"], account, snapshot, now())
    body = json.dumps(event.to_dict()).encode()
    cfg = settings.get()
    headers = webhooks.build_headers([cfg.webhook_secret], webhook_id, body)
    headers["content-type"] = "application/json"
    try:
        response = _http().post(cfg.receiver_webhook_url, content=body, headers=headers)
        status: int | None = response.status_code
    except httpx.HTTPError as exc:
        status = None
        logger.warning("delivery.failed", extra={"reason": type(exc).__name__})
    if status is not None and 200 <= status < 300:
        logger.info("delivery.succeeded", extra={"status": status, "version": event.version})
        return True
    if status is not None:
        logger.warning("delivery.failed", extra={"reason": f"http_{status}"})
    return False


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        try:
            done = deliver(record)
        except Exception:
            log.get_logger().exception("delivery.error")
            done = False
        if done:
            continue
        failures.append({"itemIdentifier": record["messageId"]})
        count = int((record.get("attributes") or {}).get("ApproximateReceiveCount", "1"))
        try:
            _sqs().change_message_visibility(
                QueueUrl=settings.get().delivery_queue_url,
                ReceiptHandle=record["receiptHandle"],
                VisibilityTimeout=backoff_seconds(count),
            )
        except Exception:
            log.get_logger().exception("delivery.backoff_failed")
    return {"batchItemFailures": failures}
