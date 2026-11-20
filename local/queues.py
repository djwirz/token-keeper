"""Drain an SQS queue into a Lambda handler, the way Lambda's event source mapping does.

Messages are passed in Lambda's record shape; the ones the handler doesn't report in
``batchItemFailures`` are deleted, and failed ones come back after their visibility
timeout (which the deliverer sets for backoff).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

import boto3

from local.http import LambdaHandler


def to_record(message: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "messageId": message["MessageId"],
        "receiptHandle": message["ReceiptHandle"],
        "body": message["Body"],
        "attributes": message.get("Attributes", {}),
        "messageAttributes": {
            name: {"stringValue": value.get("StringValue"), "dataType": value["DataType"]}
            for name, value in message.get("MessageAttributes", {}).items()
        },
        "eventSource": "aws:sqs",
    }


def drain_once(queue_url: str, handler: LambdaHandler, wait_seconds: int = 1) -> int:
    """Receive one batch and hand it to the handler. Returns how many were received."""
    sqs = boto3.client("sqs")
    messages = sqs.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=10,
        WaitTimeSeconds=wait_seconds,
        # What Lambda's SQS records carry, and the deliverer reads.
        AttributeNames=["ApproximateReceiveCount", "SentTimestamp"],
        MessageAttributeNames=["All"],
    ).get("Messages", [])
    if not messages:
        return 0
    result = handler({"Records": [to_record(m) for m in messages]}, None)
    failed = {f["itemIdentifier"] for f in result.get("batchItemFailures", [])}
    for message in messages:
        if message["MessageId"] not in failed:
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
    return len(messages)


def drain_in_thread(queue_url: str, handler: LambdaHandler) -> threading.Event:
    """Poll until the returned event is set."""
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                drain_once(queue_url, handler)
            except Exception as exc:  # keep the runner alive; the handler logs details
                print(f"  queue drain error: {type(exc).__name__}")
                stop.wait(1)

    threading.Thread(target=loop, daemon=True).start()
    return stop
