"""Sync scheduling (spec §4 "Sync cadence", ADR 0004, ADR 0015) and the sync queue.

A sync message carries only the account id, plus the correlation id as a message
attribute (spec §9: never a token).
"""

from __future__ import annotations

import json
import secrets
from typing import TYPE_CHECKING

import boto3

from token_keeper.delivery import CORRELATION_ATTRIBUTE

if TYPE_CHECKING:
    from mypy_boto3_sqs import SQSClient

SYNC_INTERVAL_SECONDS = 6 * 60 * 60
JITTER_SECONDS = 30 * 60
# The planner pushes a due account this far ahead before enqueueing it, so the next
# run (every 15 minutes) doesn't enqueue it again while it's still in the queue.
PLANNER_PUSH_SECONDS = 30 * 60
# Refresh first when less than this much access-token life is left (spec §4).
REFRESH_WINDOW_SECONDS = 10 * 60
# Transient errors: 15, 30, then 60 minutes. The planner runs every 15 minutes, so
# finer steps would buy nothing.
FIRST_BACKOFF_SECONDS = 15 * 60
MAX_BACKOFF_SECONDS = 60 * 60


def next_sync_at(now: int) -> int:
    """Six hours on, give or take half an hour, so accounts spread out."""
    return now + SYNC_INTERVAL_SECONDS + secrets.randbelow(2 * JITTER_SECONDS + 1) - JITTER_SECONDS


def backoff_until(now: int, consecutive_failures: int) -> int:
    """When to retry after the ``consecutive_failures``-th transient error in a row (1-based)."""
    step = FIRST_BACKOFF_SECONDS * int(2 ** max(consecutive_failures - 1, 0))
    return now + min(step, MAX_BACKOFF_SECONDS)


def rate_limited_until(now: int, reset_at: int) -> int:
    """GitHub's reset time, kept between a minute and the backoff cap."""
    return min(max(reset_at, now + 60), now + MAX_BACKOFF_SECONDS)


def enqueue(
    queue_url: str,
    account_ids: list[str],
    *,
    correlation_id: str,
    client: SQSClient | None = None,
) -> None:
    """Send one message per account, ten at a time (SQS's batch limit)."""
    sqs: SQSClient = client or boto3.client("sqs")
    attributes = {CORRELATION_ATTRIBUTE: {"DataType": "String", "StringValue": correlation_id}}
    for start in range(0, len(account_ids), 10):
        batch = account_ids[start : start + 10]
        response = sqs.send_message_batch(
            QueueUrl=queue_url,
            Entries=[
                {
                    "Id": str(i),
                    "MessageBody": json.dumps({"account_id": account_id}),
                    "MessageAttributes": attributes,  # type: ignore[typeddict-item]
                }
                for i, account_id in enumerate(batch)
            ],
        )
        if response.get("Failed"):
            # The planner already pushed next_due_at, so these come back in about 30 min.
            raise RuntimeError(f"sync enqueue failed for {len(response['Failed'])} accounts")
