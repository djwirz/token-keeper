"""Run the demo receiver and Token Keeper locally against an in-process moto server.

    uv run python -m local.run

- Receiver: http://localhost:8000 (status page, webhook endpoint, passcode connect)
- Token Keeper: http://localhost:8001 (``/connect``, ``/callback``), plus a thread that
  drains the delivery queue into the deliverer, as Lambda's SQS trigger would.
- AWS calls go to moto (DynamoDB, SQS, KMS, SSM) via AWS_ENDPOINT_URL, so handler code
  has no local-only branches. GitHub is the real one, through Dan's GitHub App.
- Dev secrets are generated into .env.local (gitignored) on first run. The GitHub App's
  ``GITHUB_CLIENT_ID`` and ``GITHUB_CLIENT_SECRET`` are added there by hand (spec §5);
  without them only the receiver starts. Data lives in memory and is gone when the
  runner stops.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import socket
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import boto3
from moto.server import ThreadedMotoServer

from local.http import make_server, router, serve_in_thread
from local.queues import drain_in_thread

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env.local"
RECEIVER_TABLE = "receiver-local"
TOKEN_KEEPER_TABLE = "token-keeper-local"  # noqa: S105 (a table name)
TOKEN_KEEPER_PORT = 8001
GITHUB_KEYS = ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET")


def load_or_create_secrets(path: Path = ENV_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    defaults = {
        "WEBHOOK_SECRET": "whsec_" + base64.b64encode(secrets.token_bytes(32)).decode(),
        "CONNECT_LINK_SECRET": secrets.token_urlsafe(32),
        "DEMO_PASSCODE": secrets.token_urlsafe(9),
    }
    missing = {k: v for k, v in defaults.items() if k not in values}
    if missing:
        values.update(missing)
        lines = ["# Local dev secrets for token-keeper. Generated; never commit."]
        lines += [f"{k}={v}" for k, v in values.items()]
        # Created owner-only from the start, so the secrets are never briefly readable.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write("\n".join(lines) + "\n")
        path.chmod(0o600)  # O_CREAT's mode doesn't apply to a file that already existed
    return values


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@dataclass
class Running:
    receiver_url: str
    token_keeper_url: str | None
    stop: Callable[[], None]


def _create_token_keeper_resources() -> dict[str, str]:
    """The table, KMS key and delivery queue (with its DLQ), on moto."""
    from token_keeper.handlers.deliverer import MAX_RECEIVES
    from token_keeper.store import ATTRIBUTE_DEFINITIONS, KEY_SCHEMA, TTL_ATTRIBUTE

    dynamodb = boto3.client("dynamodb")
    dynamodb.create_table(
        TableName=TOKEN_KEEPER_TABLE,
        KeySchema=KEY_SCHEMA,  # type: ignore[arg-type]
        AttributeDefinitions=ATTRIBUTE_DEFINITIONS,  # type: ignore[arg-type]
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.update_time_to_live(
        TableName=TOKEN_KEEPER_TABLE,
        TimeToLiveSpecification={"Enabled": True, "AttributeName": TTL_ATTRIBUTE},
    )
    key_id = boto3.client("kms").create_key(Description="token-keeper-local")["KeyMetadata"][
        "KeyId"
    ]
    sqs = boto3.client("sqs")
    dlq_url = sqs.create_queue(QueueName="delivery-dlq-local")["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    queue_url = sqs.create_queue(
        QueueName="delivery-local",
        Attributes={
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": str(MAX_RECEIVES)}
            )
        },
    )["QueueUrl"]
    return {
        "TOKEN_KEEPER_TABLE": TOKEN_KEEPER_TABLE,
        "KMS_KEY_ID": key_id,
        "DELIVERY_QUEUE_URL": queue_url,
    }


def start(
    receiver_port: int = 8000,
    env_file: Path = ENV_FILE,
    token_keeper_port: int = TOKEN_KEEPER_PORT,
) -> Running:
    """Start moto, the receiver and (with GitHub App credentials) Token Keeper.

    Sets process environment variables.
    """
    moto_port = free_port()
    moto = ThreadedMotoServer(ip_address="127.0.0.1", port=moto_port)
    moto.start()

    endpoint = f"http://127.0.0.1:{moto_port}"
    receiver_url = f"http://localhost:{receiver_port}/"
    token_keeper_url = f"http://localhost:{token_keeper_port}"
    values = load_or_create_secrets(env_file)
    os.environ.update(
        {
            "AWS_ENDPOINT_URL": endpoint,
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_ACCESS_KEY_ID": "local",
            "AWS_SECRET_ACCESS_KEY": "local",
            "RECEIVER_TABLE": RECEIVER_TABLE,
            "TOKEN_KEEPER_CONNECT_URL": f"{token_keeper_url}/connect",
            "RECEIVER_PUBLIC_URL": receiver_url,
            **values,
        }
    )

    from receiver.store import ATTRIBUTE_DEFINITIONS, KEY_SCHEMA

    dynamodb = boto3.client("dynamodb")
    dynamodb.create_table(
        TableName=RECEIVER_TABLE,
        KeySchema=KEY_SCHEMA,  # type: ignore[arg-type]
        AttributeDefinitions=ATTRIBUTE_DEFINITIONS,  # type: ignore[arg-type]
        BillingMode="PAY_PER_REQUEST",
    )

    from receiver.handler import handler

    servers = [make_server(handler, "127.0.0.1", receiver_port)]
    drains = []
    with_token_keeper = all(values.get(k) for k in GITHUB_KEYS)
    if with_token_keeper:
        os.environ.update(
            {
                **_create_token_keeper_resources(),
                "TOKEN_KEEPER_PUBLIC_URL": token_keeper_url,
                "ALLOWED_RETURN_TO": json.dumps({"demo": [receiver_url]}),
                "RECEIVER_WEBHOOK_URL": f"{receiver_url}webhooks/token-keeper",
            }
        )
        from token_keeper.handlers import _shared, callback, connect, deliverer

        _shared.reset()
        token_keeper = router(
            {("GET", "/connect"): connect.handler, ("GET", "/callback"): callback.handler}
        )
        servers.append(make_server(token_keeper, "127.0.0.1", token_keeper_port))
        drains.append(drain_in_thread(os.environ["DELIVERY_QUEUE_URL"], deliverer.handler))
    for server in servers:
        serve_in_thread(server)

    def stop() -> None:
        for drain in drains:
            drain.set()
        for server in servers:
            server.shutdown()
            server.server_close()
        # moto keeps its data in process-wide backends; clear them so a later start
        # in the same process (the tests) begins empty.
        reset = urllib.request.Request(f"{endpoint}/moto-api/reset", method="POST")  # noqa: S310
        urllib.request.urlopen(reset, timeout=5).close()  # noqa: S310 (local moto, http only)
        moto.stop()

    return Running(
        receiver_url=receiver_url,
        token_keeper_url=token_keeper_url if with_token_keeper else None,
        stop=stop,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    running = start(args.port)
    print(f"Receiver: {running.receiver_url}")
    if running.token_keeper_url:
        print(f"Token Keeper: {running.token_keeper_url}")
        if os.environ["GITHUB_CLIENT_ID"].isdigit():
            # GitHub answers an unknown client_id with a bare 404 after sign-in.
            print(
                "Warning: GITHUB_CLIENT_ID is all digits, which is the GitHub App's App ID. "
                "Use its Client ID (it starts with 'Iv')."
            )
    else:
        print(
            "Token Keeper not started: add GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET from "
            f"your GitHub App to {ENV_FILE.name} (see README)."
        )
    print(f"Passcode and secrets: {ENV_FILE.name}   (Ctrl-C to stop)")
    try:
        import threading

        threading.Event().wait()
    except KeyboardInterrupt:
        running.stop()


if __name__ == "__main__":
    main()
