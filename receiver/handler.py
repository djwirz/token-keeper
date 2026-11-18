"""Demo receiver: the stand-in "main app" (spec §1, §6).

One Lambda behind an API Gateway HTTP API (payload format 2.0):
- ``GET /``: public, read-only status page
- ``POST /webhooks/token-keeper``: signed account events from Token Keeper
- ``POST /connect``: passcode-gated; redirects to a signed Token Keeper connect link
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
from functools import cache
from typing import Any
from urllib.parse import parse_qs

from jinja2 import Environment, PackageLoader, select_autoescape

from receiver import config
from receiver.store import Store
from token_keeper.contract import connect_links, webhooks
from token_keeper.contract.events import EventParseError, parse_event

logger = logging.getLogger("receiver")
logger.setLevel(logging.INFO)

WEBHOOK_PATH = "/webhooks/token-keeper"
RESULT_MESSAGES = {
    "connected": "GitHub account connected.",
    "denied": "Connection cancelled on GitHub.",
    "already_linked": "That GitHub account is already linked to another user.",
}
SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'none'; img-src https:; style-src 'unsafe-inline'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
}

Response = dict[str, Any]


@cache
def _templates() -> Environment:
    return Environment(
        loader=PackageLoader("receiver", "templates"),
        autoescape=select_autoescape(["html"]),
    )


@cache
def _store() -> Store:
    return Store(config.load().table_name)


def _log(event: str, **fields: Any) -> None:
    logger.info(json.dumps({"event": event, **fields}))


def _html(status: int, body: str) -> Response:
    return {
        "statusCode": status,
        "headers": {"content-type": "text/html; charset=utf-8", **SECURITY_HEADERS},
        "body": body,
    }


def _empty(status: int) -> Response:
    return {"statusCode": status, "headers": dict(SECURITY_HEADERS), "body": ""}


def _body(event: dict[str, Any]) -> bytes:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(raw)
    return str(raw).encode()


def _status_page(event: dict[str, Any], *, status: int = 200, error: str | None = None) -> Response:
    result = (event.get("queryStringParameters") or {}).get("result")
    page = (
        _templates()
        .get_template("status.html")
        .render(
            accounts=_store().list_accounts(),
            message=RESULT_MESSAGES.get(result or ""),
            error=error,
        )
    )
    return _html(status, page)


def _receive_webhook(event: dict[str, Any]) -> Response:
    cfg = config.load()
    body = _body(event)
    try:
        webhook_id = webhooks.verify(cfg.webhook_secret, event.get("headers") or {}, body)
    except webhooks.WebhookVerificationError as exc:
        _log("webhook.rejected", reason=exc.reason)
        return _empty(401)
    try:
        account_event = parse_event(json.loads(body))
    except (ValueError, EventParseError) as exc:
        reason = exc.reason if isinstance(exc, EventParseError) else "bad_json"
        _log("webhook.rejected", reason=reason, webhook_id=webhook_id)
        return _empty(400)
    result = _store().save_event(webhook_id, account_event)
    _log(
        "webhook.received",
        result=result,
        webhook_id=webhook_id,
        account_id=account_event.account_id,
        version=account_event.version,
    )
    return _empty(204)


def _connect(event: dict[str, Any]) -> Response:
    cfg = config.load()
    form = parse_qs(_body(event).decode(errors="replace"))
    passcode = (form.get("passcode") or [""])[0]
    if not hmac.compare_digest(passcode.encode(), cfg.passcode.encode()):
        _log("connect.rejected", reason="bad_passcode")
        return _status_page(event, status=403, error="Wrong passcode.")
    link = connect_links.build_link(
        cfg.token_keeper_connect_url,
        cfg.connect_link_secret,
        app_id=cfg.app_id,
        external_user_id=cfg.demo_user,
        return_to=cfg.public_url,
    )
    _log("connect.redirect", external_user_id=cfg.demo_user)
    return {"statusCode": 303, "headers": {"location": link, **SECURITY_HEADERS}, "body": ""}


ROUTES = {
    ("GET", "/"): _status_page,
    ("POST", WEBHOOK_PATH): _receive_webhook,
    ("POST", "/connect"): _connect,
}


def handler(event: dict[str, Any], context: object) -> Response:
    method = event["requestContext"]["http"]["method"]
    route = ROUTES.get((method, event.get("rawPath", "")))
    if route is None:
        return _empty(404)
    return route(event)
