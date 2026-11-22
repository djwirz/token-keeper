"""Sync scheduling (spec §4 "Sync cadence", ADR 0004, ADR 0015) and the sync queue.

A sync message carries only the account id, plus the correlation id as a message
attribute (spec §9: never a token).
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from token_keeper import log
from token_keeper.delivery import CORRELATION_ATTRIBUTE
from token_keeper.ssm import optional_env_or_ssm

if TYPE_CHECKING:
    from mypy_boto3_sqs import SQSClient

DEFAULT_SYNC_INTERVAL_SECONDS = 6 * 60 * 60
# The demo cadence knob (ADR 0020): minutes, from SYNC_INTERVAL_MINUTES locally or the
# parameter SYNC_INTERVAL_MINUTES_PARAM names in AWS. Never below the planner's 15-minute
# rate (a shorter value would buy nothing) and never above the normal six hours.
SYNC_INTERVAL_SETTING = "SYNC_INTERVAL_MINUTES"
MIN_SYNC_INTERVAL_MINUTES = 15
MAX_SYNC_INTERVAL_MINUTES = DEFAULT_SYNC_INTERVAL_SECONDS // 60
# Re-read at most this often, so a change lands within a minute without a cold start.
INTERVAL_CACHE_SECONDS = 60
# The knob is read before the lease is taken, but a hung read would still eat the
# worker's 40 seconds: fail fast and fall back to six hours instead (PR 11 review).
INTERVAL_READ_CONFIG = Config(connect_timeout=2, read_timeout=2, retries={"max_attempts": 2})
# Jitter is ±30 minutes, or a quarter of the interval when that's smaller, so a short
# demo cadence isn't swamped by it.
MAX_JITTER_SECONDS = 30 * 60
# The planner pushes a due account this far ahead before enqueueing it, so the next
# run (every 15 minutes) doesn't enqueue it again while it's still in the queue.
PLANNER_PUSH_SECONDS = 30 * 60
# Refresh first when less than this much access-token life is left (spec §4).
REFRESH_WINDOW_SECONDS = 10 * 60
# Transient errors: 15, 30, then 60 minutes. The planner runs every 15 minutes, so
# finer steps would buy nothing.
FIRST_BACKOFF_SECONDS = 15 * 60
MAX_BACKOFF_SECONDS = 60 * 60


_interval_cache: tuple[float, int] | None = None  # (when it was read, seconds)
# Wall-clock time, not monotonic: whether Lambda's monotonic clock advances while the
# sandbox is frozen isn't documented, and a 60-second cache doesn't care about clock
# jumps. A module attribute, so tests can move it for the path the worker runs.
_now: Callable[[], float] = time.time


def jitter_seconds(interval: int) -> int:
    return min(MAX_JITTER_SECONDS, interval // 4)


def sync_interval_seconds() -> int:
    """The current sync interval, re-read at most once a minute. The fallback is cached
    too, so a bad knob logs a warning at most once a minute per warm worker."""
    global _interval_cache
    read_at = _now()
    if _interval_cache is not None and read_at - _interval_cache[0] < INTERVAL_CACHE_SECONDS:
        return _interval_cache[1]
    interval = _read_interval()
    _interval_cache = (read_at, interval)
    return interval


def reset_interval_cache() -> None:
    global _interval_cache
    _interval_cache = None


def _read_interval() -> int:
    """The knob's value in seconds. A missing, unreadable or bad knob never stops syncs:
    it means the normal six hours."""
    try:
        raw = optional_env_or_ssm(SYNC_INTERVAL_SETTING, INTERVAL_READ_CONFIG)
    except (BotoCoreError, ClientError) as exc:
        log.get_logger().warning("sync.interval_unreadable", extra={"error": type(exc).__name__})
        return DEFAULT_SYNC_INTERVAL_SECONDS
    if raw is None:
        return DEFAULT_SYNC_INTERVAL_SECONDS
    # Whole minutes in plain ASCII digits: int() alone would also take "+15" or "1_5".
    digits = raw.strip()
    minutes = int(digits) if digits.isascii() and digits.isdigit() else 0
    if not MIN_SYNC_INTERVAL_MINUTES <= minutes <= MAX_SYNC_INTERVAL_MINUTES:
        log.get_logger().warning("sync.interval_invalid", extra={"value": raw[:16]})
        return DEFAULT_SYNC_INTERVAL_SECONDS
    return minutes * 60


def next_sync_at(now: int, interval: int | None = None) -> int:
    """One interval on (six hours unless the demo knob says otherwise), with jitter so
    accounts spread out."""
    if interval is None:
        interval = sync_interval_seconds()
    jitter = jitter_seconds(interval)
    return now + interval + secrets.randbelow(2 * jitter + 1) - jitter


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
