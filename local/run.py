"""Run the demo receiver locally against an in-process moto server.

    uv run python -m local.run

- Receiver: http://localhost:8000 (status page, webhook endpoint, passcode connect)
- Token Keeper will listen on :8001 from slice 2; until then the connect redirect 404s.
- AWS calls go to moto via AWS_ENDPOINT_URL, so handler code has no local-only branches.
- Dev secrets are generated into .env.local (gitignored) on first run. Data lives in
  memory and is gone when the runner stops.
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import boto3
from moto.server import ThreadedMotoServer

from local.http import make_server, serve_in_thread

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env.local"
RECEIVER_TABLE = "receiver-local"
TOKEN_KEEPER_PORT = 8001


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
        path.write_text("\n".join(lines) + "\n")
        path.chmod(0o600)
    return values


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@dataclass
class Running:
    receiver_url: str
    stop: Callable[[], None]


def start(receiver_port: int = 8000, env_file: Path = ENV_FILE) -> Running:
    """Start moto and the receiver. Sets process environment variables."""
    moto_port = free_port()
    moto = ThreadedMotoServer(ip_address="127.0.0.1", port=moto_port)
    moto.start()

    receiver_url = f"http://localhost:{receiver_port}/"
    os.environ.update(
        {
            "AWS_ENDPOINT_URL": f"http://127.0.0.1:{moto_port}",
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_ACCESS_KEY_ID": "local",
            "AWS_SECRET_ACCESS_KEY": "local",
            "RECEIVER_TABLE": RECEIVER_TABLE,
            "TOKEN_KEEPER_CONNECT_URL": f"http://localhost:{TOKEN_KEEPER_PORT}/connect",
            "RECEIVER_PUBLIC_URL": receiver_url,
            **load_or_create_secrets(env_file),
        }
    )

    from receiver.store import ATTRIBUTE_DEFINITIONS, KEY_SCHEMA, TTL_ATTRIBUTE

    dynamodb = boto3.client("dynamodb")
    dynamodb.create_table(
        TableName=RECEIVER_TABLE,
        KeySchema=KEY_SCHEMA,  # type: ignore[arg-type]
        AttributeDefinitions=ATTRIBUTE_DEFINITIONS,  # type: ignore[arg-type]
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.update_time_to_live(
        TableName=RECEIVER_TABLE,
        TimeToLiveSpecification={"Enabled": True, "AttributeName": TTL_ATTRIBUTE},
    )

    from receiver.handler import handler

    server = make_server(handler, "127.0.0.1", receiver_port)
    serve_in_thread(server)

    def stop() -> None:
        server.shutdown()
        server.server_close()
        moto.stop()

    return Running(receiver_url=receiver_url, stop=stop)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    running = start(args.port)
    print(f"Receiver: {running.receiver_url}")
    print(f"Passcode and secrets: {ENV_FILE.name}")
    print("Send an event: uv run python -m local.send_event   (Ctrl-C to stop)")
    try:
        import threading

        threading.Event().wait()
    except KeyboardInterrupt:
        running.stop()


if __name__ == "__main__":
    main()
