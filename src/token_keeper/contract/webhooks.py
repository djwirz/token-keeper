"""Standard Webhooks signing and verification (spec §6.3, ADR 0005).

Signed content is ``{webhook-id}.{webhook-timestamp}.{body}``. Secrets use the
``whsec_<base64>`` format; the HMAC key is the base64-decoded part. The signature
header holds one or more space-separated ``v1,<base64>`` values so secrets can be
rotated without downtime.

See https://www.standardwebhooks.com/ and the spec in
https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from collections.abc import Iterable, Mapping

SECRET_PREFIX = "whsec_"  # noqa: S105 (format prefix, not a secret)
SIGNATURE_VERSION = "v1"
DEFAULT_TOLERANCE_SECONDS = 5 * 60

HEADER_ID = "webhook-id"
HEADER_TIMESTAMP = "webhook-timestamp"
HEADER_SIGNATURE = "webhook-signature"


class WebhookVerificationError(Exception):
    """The request is not a valid, fresh, correctly signed webhook.

    ``reason`` is a short code that is safe to log; it never contains request data.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _key(secret: str) -> bytes:
    raw = secret.removeprefix(SECRET_PREFIX)
    try:
        return base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise ValueError("webhook secret is not valid base64") from exc


def sign(secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    """Return one ``v1,<base64>`` signature for this message."""
    signed = f"{msg_id}.{timestamp}.".encode() + body
    digest = hmac.new(_key(secret), signed, hashlib.sha256).digest()
    return f"{SIGNATURE_VERSION},{base64.b64encode(digest).decode()}"


def build_headers(
    secrets: Iterable[str], msg_id: str, body: bytes, timestamp: int | None = None
) -> dict[str, str]:
    """Headers for one delivery attempt, signed with every active secret."""
    ts = int(time.time()) if timestamp is None else timestamp
    signatures = " ".join(sign(s, msg_id, ts, body) for s in secrets)
    if not signatures:
        raise ValueError("at least one secret is required")
    return {HEADER_ID: msg_id, HEADER_TIMESTAMP: str(ts), HEADER_SIGNATURE: signatures}


def verify(
    secret: str,
    headers: Mapping[str, str],
    body: bytes,
    *,
    now: int | None = None,
    tolerance: int = DEFAULT_TOLERANCE_SECONDS,
) -> str:
    """Verify a delivery and return its ``webhook-id``.

    Header names are matched case-insensitively. Raises ``WebhookVerificationError``.
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    msg_id = lowered.get(HEADER_ID)
    ts_raw = lowered.get(HEADER_TIMESTAMP)
    sig_header = lowered.get(HEADER_SIGNATURE)
    if not msg_id or not ts_raw or not sig_header:
        raise WebhookVerificationError("missing_header")

    try:
        timestamp = int(ts_raw)
    except ValueError:
        raise WebhookVerificationError("bad_timestamp") from None
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > tolerance:
        raise WebhookVerificationError("timestamp_out_of_range")

    expected = sign(secret, msg_id, timestamp, body).split(",", 1)[1]
    for candidate in sig_header.split(" "):
        version, _, value = candidate.partition(",")
        if version != SIGNATURE_VERSION or not value:
            continue
        if hmac.compare_digest(value.encode(), expected.encode()):
            return msg_id
    raise WebhookVerificationError("bad_signature")
