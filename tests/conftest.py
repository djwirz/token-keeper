from __future__ import annotations

from collections.abc import Iterator
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
CONNECT_URL = "http://localhost:8000/connect"


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
