"""Enqueue an account event for the deliverer (spec §4, §6.3).

The message carries ids only: never a token, and not the event body either, because
the deliverer reads the account's full current state when it sends (spec §6.3).
``webhook_id`` is fixed here, so it stays the same across delivery retries.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import boto3

from token_keeper import ids

if TYPE_CHECKING:
    from mypy_boto3_sqs import SQSClient

CORRELATION_ATTRIBUTE = "correlation_id"


def enqueue(
    queue_url: str,
    *,
    account_id: str,
    event_type: str,
    correlation_id: str,
    client: SQSClient | None = None,
) -> str:
    sqs: SQSClient = client or boto3.client("sqs")
    webhook_id = ids.new("msg")
    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(
            {"account_id": account_id, "type": event_type, "webhook_id": webhook_id}
        ),
        MessageAttributes={
            CORRELATION_ATTRIBUTE: {"DataType": "String", "StringValue": correlation_id}
        },
    )
    return webhook_id
