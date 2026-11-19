"""Sign and send a synthetic account event to the local receiver.

    uv run python -m local.send_event --version 2 --name "Octo Demo"
    uv run python -m local.send_event --version 3 --state needs_reauth --reason revoked

Stands in for Token Keeper's deliverer until slice 2. The data is made up.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

from local.run import ENV_FILE, load_or_create_secrets
from token_keeper.contract import webhooks


def synthetic_event(
    version: int, state: str, reason: str | None, name: str, login: str
) -> dict[str, Any]:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "type": "account.updated",
        "app_id": "demo",
        "account_id": "acc_local_demo",
        "external_user_id": "u_demo",
        "version": version,
        "state": state,
        "state_reason": reason,
        "provider": "github",
        "github": {"user_id": 1, "login": login},
        "snapshot": {
            "fetched_at": now,
            "profile": {"name": name, "avatar_url": None, "public_repos": 8, "followers": 3},
            "recent_events": [
                {"type": "PushEvent", "repo": f"{login}/hello", "created_at": now},
            ],
        },
        "occurred_at": now,
    }


def send(url: str, secret: str, event: dict[str, Any], msg_id: str) -> int:
    body = json.dumps(event).encode()
    headers = webhooks.build_headers([secret], msg_id, body)
    headers["content-type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a signed synthetic event.")
    parser.add_argument("--url", default="http://localhost:8000/webhooks/token-keeper")
    parser.add_argument("--version", type=int, default=1)
    parser.add_argument("--id", default=None, help="webhook-id (default: a new one)")
    parser.add_argument("--state", default="active", choices=["active", "needs_reauth"])
    parser.add_argument("--reason", default=None)
    parser.add_argument("--name", default="Octo Demo")
    parser.add_argument("--login", default="octo-demo")
    args = parser.parse_args()

    secret = load_or_create_secrets(ENV_FILE)["WEBHOOK_SECRET"]
    msg_id = args.id or f"msg_local_{time.time_ns()}"
    event = synthetic_event(args.version, args.state, args.reason, args.name, args.login)
    status = send(args.url, secret, event, msg_id)
    print(f"{msg_id} version={args.version} -> HTTP {status}")


if __name__ == "__main__":
    main()
