"""Drive Token Keeper's handlers the way API Gateway and SQS would."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import boto3

from local.queues import drain_once
from tests.conftest import CONNECT_SECRET, PUBLIC_URL, TK_PUBLIC_URL, http_event
from token_keeper.contract import connect_links
from token_keeper.handlers import callback, connect, deliverer


def signed_query(external_user_id: str = "u_demo", **overrides: Any) -> str:
    link = connect_links.build_link(
        f"{TK_PUBLIC_URL}/connect",
        overrides.pop("secret", CONNECT_SECRET),
        app_id=overrides.pop("app_id", "demo"),
        external_user_id=external_user_id,
        return_to=overrides.pop("return_to", PUBLIC_URL),
        **overrides,
    )
    return urlsplit(link).query


def connect_event(raw_query: str) -> dict[str, Any]:
    event = http_event("GET", "/connect")
    event["rawQueryString"] = raw_query
    event["queryStringParameters"] = {k: v[0] for k, v in parse_qs(raw_query).items()}
    return event


def start_connect(external_user_id: str = "u_demo") -> tuple[dict[str, Any], dict[str, str]]:
    """Run /connect; return the response and the GitHub authorize URL's parameters."""
    response = connect.handler(connect_event(signed_query(external_user_id)), None)
    assert response["statusCode"] == 302, response
    location = response["headers"]["location"]
    params = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
    return response, params


def run_callback(query: dict[str, str]) -> dict[str, Any]:
    return callback.handler(http_event("GET", "/callback", query=query), None)


def connect_and_callback(
    external_user_id: str = "u_demo", code: str = "test-code"
) -> dict[str, Any]:
    _, params = start_connect(external_user_id)
    return run_callback({"code": code, "state": params["state"]})


def drain(queue_url: str) -> int:
    return drain_once(queue_url, deliverer.handler, wait_seconds=0)


def scan_table(table: str) -> list[dict[str, Any]]:
    return list(boto3.client("dynamodb").scan(TableName=table)["Items"])


def queue_messages(queue_url: str) -> list[Any]:
    return list(
        boto3.client("sqs")
        .receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=10,
            MessageAttributeNames=["All"],
            VisibilityTimeout=0,
        )
        .get("Messages", [])
    )
