"""``POST /github/webhook``: GitHub App deliveries (spec §6.4, ADR 0010, ADR 0018).

1. Verify ``X-Hub-Signature-256`` over the raw body before reading anything else.
   Missing or bad → 401, nothing parsed.
2. Act only on a revocation: the ``X-GitHub-Event`` header is
   ``github_app_authorization`` *and* the body says ``"action": "revoked"``. The
   headers aren't signed, so the body check stops a signed body from another event
   (a ``ping``, say) being replayed under a revocation's event name.
3. Look up ``sender.id`` through the ``GHUSER#`` link, make the takeover write to
   ``needs_reauth`` (``revoked``), which deletes the tokens and clears any lease, and
   enqueue ``account.updated``.

Unknown users and other events get a 204 and a log line. A redelivery makes no write
(the store's condition), but still enqueues an event: the deliverer sends the current
state, the receiver ignores an equal version, and an event lost on the first attempt
is sent after all. GitHub never redelivers on its own, so a 5xx here only shows up as a
failure in the App's Recent Deliveries.

The body is never logged: only the delivery GUID, the event name and the outcome.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any

from token_keeper import delivery, github, ids, log, settings
from token_keeper.handlers._shared import store

REVOCATION_EVENT = "github_app_authorization"
REVOKED = "revoked"
# GitHub's delivery GUIDs; anything else isn't stored or logged.
_GUID = re.compile(r"[0-9a-fA-F-]{8,64}")

Response = dict[str, Any]


class _BadRequest(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _empty(status: int) -> Response:
    return {"statusCode": status, "headers": {"cache-control": "no-store"}, "body": ""}


def _raw_body(event: dict[str, Any]) -> bytes:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            return base64.b64decode(raw, validate=True)
        except binascii.Error:
            raise _BadRequest("bad_body_encoding") from None
    return str(raw).encode()


def _payload(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except ValueError:
        raise _BadRequest("bad_json") from None
    if not isinstance(payload, dict):
        raise _BadRequest("bad_json")
    return payload


def _sender_id(payload: dict[str, Any]) -> int:
    sender = payload.get("sender")
    sender_id = sender.get("id") if isinstance(sender, dict) else None
    # bool is an int in Python; a sender id never is one.
    if not isinstance(sender_id, int) or isinstance(sender_id, bool):
        raise _BadRequest("bad_sender")
    return sender_id


def handler(event: dict[str, Any], context: object) -> Response:
    correlation_id = ids.new("cor")
    logger = log.start(handler="github_webhook", correlation_id=correlation_id)
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    guid = headers.get("x-github-delivery") or ""
    guid = guid if _GUID.fullmatch(guid) else None
    event_name = github.safe_code(headers.get("x-github-event"))
    log.add_keys(github_delivery=guid, github_event=event_name)

    try:
        body = _raw_body(event)
        github.verify_webhook_signature(
            settings.get().github_webhook_secret, headers.get("x-hub-signature-256"), body
        )
    except github.WebhookSignatureError as exc:
        logger.info("github_webhook.rejected", extra={"reason": exc.reason})
        return _empty(401)
    except _BadRequest as exc:
        # A body that can't be decoded can't be verified either: 401, as for a bad
        # signature (spec §6.4), not 400 (PR 6 review).
        logger.info("github_webhook.rejected", extra={"reason": exc.reason})
        return _empty(401)

    # Signed by GitHub from here on (the body, not the headers).
    try:
        payload = _payload(body)
        action = payload.get("action")
        if event_name != REVOCATION_EVENT or action != REVOKED:
            logger.info(
                "github_webhook.ignored",
                extra={"reason": "ignored_event", "action": github.safe_code(action)},
            )
            return _empty(204)
        sender_id = _sender_id(payload)
    except _BadRequest as exc:
        logger.info("github_webhook.rejected", extra={"reason": exc.reason})
        return _empty(400)

    account_id = store().account_for_github_user(sender_id)
    if account_id is None:
        logger.info("github_webhook.ignored", extra={"reason": "unknown_user"})
        return _empty(204)
    log.add_keys(account_id=account_id)
    written = store().mark_needs_reauth_takeover(account_id, reason=REVOKED, delivery=guid)
    webhook_id = delivery.enqueue(
        settings.get().delivery_queue_url,
        account_id=account_id,
        event_type="account.updated",
        correlation_id=correlation_id,
    )
    logger.info(
        "github_webhook.revoked" if written else "github_webhook.already_revoked",
        extra={"webhook_id": webhook_id},
    )
    return _empty(204)
