from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import boto3
import pytest
from moto import mock_aws

from receiver import config, handler
from receiver.store import ATTRIBUTE_DEFINITIONS, KEY_SCHEMA

TABLE = "receiver-test"
WEBHOOK_SECRET = "whsec_" + "dGVzdC13ZWJob29rLXNlY3JldC1ub3QtcmVhbA=="
CONNECT_SECRET = "test-connect-link-secret-not-real"
PASSCODE = "test-passcode"
PUBLIC_URL = "http://localhost:8000/"
# A different origin from PUBLIC_URL, as in the runner and in AWS: the CSP must allow it.
CONNECT_URL = "http://localhost:8001/connect"


def create_table(name: str = TABLE) -> None:
    client = boto3.client("dynamodb")
    client.create_table(
        TableName=name,
        KeySchema=KEY_SCHEMA,  # type: ignore[arg-type]
        AttributeDefinitions=ATTRIBUTE_DEFINITIONS,  # type: ignore[arg-type]
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def receiver_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    env = {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "RECEIVER_TABLE": TABLE,
        "WEBHOOK_SECRET": WEBHOOK_SECRET,
        "CONNECT_LINK_SECRET": CONNECT_SECRET,
        "DEMO_PASSCODE": PASSCODE,
        "TOKEN_KEEPER_CONNECT_URL": CONNECT_URL,
        "RECEIVER_PUBLIC_URL": PUBLIC_URL,
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    config.load.cache_clear()
    handler._store.cache_clear()
    with mock_aws():
        create_table()
        yield
    config.load.cache_clear()
    handler._store.cache_clear()


def http_event(
    method: str,
    path: str,
    *,
    body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A minimal API Gateway HTTP API (payload 2.0) event."""
    return {
        "version": "2.0",
        "rawPath": path,
        "rawQueryString": "&".join(f"{k}={v}" for k, v in (query or {}).items()),
        "queryStringParameters": query,
        "headers": {k.lower(): v for k, v in (headers or {}).items()},
        "requestContext": {"http": {"method": method, "path": path}},
        "body": body,
        "isBase64Encoded": False,
    }


# Token Keeper ------------------------------------------------------------------

TK_TABLE = "token-keeper-test"
TK_PUBLIC_URL = "http://localhost:8001"
RECEIVER_WEBHOOK_URL = "http://localhost:8000/webhooks/token-keeper"
GITHUB_CLIENT_ID = "Iv23-test-client-id"
GITHUB_CLIENT_SECRET = "test-client-secret-not-real"


@dataclass(frozen=True)
class TokenKeeperEnv:
    queue_url: str
    dlq_url: str
    key_id: str


@pytest.fixture
def tk_env(receiver_env: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[TokenKeeperEnv]:
    """Token Keeper's table, key and queues on moto, inside receiver_env's mock_aws."""
    from token_keeper.handlers import _shared, deliverer
    from token_keeper.store import ATTRIBUTE_DEFINITIONS as TK_ATTRS
    from token_keeper.store import KEY_SCHEMA as TK_KEYS

    boto3.client("dynamodb").create_table(
        TableName=TK_TABLE,
        KeySchema=TK_KEYS,  # type: ignore[arg-type]
        AttributeDefinitions=TK_ATTRS,  # type: ignore[arg-type]
        BillingMode="PAY_PER_REQUEST",
    )
    key_id = boto3.client("kms").create_key()["KeyMetadata"]["KeyId"]
    sqs = boto3.client("sqs")
    dlq_url = sqs.create_queue(QueueName="delivery-dlq")["QueueUrl"]
    queue_url = sqs.create_queue(QueueName="delivery")["QueueUrl"]
    env = {
        "TOKEN_KEEPER_TABLE": TK_TABLE,
        "KMS_KEY_ID": key_id,
        "DELIVERY_QUEUE_URL": queue_url,
        "TOKEN_KEEPER_PUBLIC_URL": TK_PUBLIC_URL,
        "GITHUB_CLIENT_ID": GITHUB_CLIENT_ID,
        "GITHUB_CLIENT_SECRET": GITHUB_CLIENT_SECRET,
        "RECEIVER_URL": PUBLIC_URL,
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    _shared.reset()
    deliverer._sqs.cache_clear()
    yield TokenKeeperEnv(queue_url=queue_url, dlq_url=dlq_url, key_id=key_id)
    _shared.reset()
    deliverer._sqs.cache_clear()
