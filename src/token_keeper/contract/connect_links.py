"""Signed connect links (spec §6.1, ADR 0007).

The main app builds ``/connect?app_id=…&external_user_id=…&return_to=…&exp=…&sig=…``.

Canonical string: every parameter except ``sig``, sorted by name, each written
``name=value`` with RFC 3986 percent-encoding (unreserved characters ``A-Z a-z 0-9 - . _ ~``
left as is, everything else UTF-8 percent-encoded with uppercase hex, so a space is
``%20``), joined with ``&``. ``sig`` is the lowercase hex HMAC-SHA256 of that string,
keyed with the UTF-8 bytes of the connect-link secret.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote

MAX_LIFETIME_SECONDS = 10 * 60
SIGNED_PARAMS = frozenset({"app_id", "external_user_id", "return_to", "exp"})
_UNRESERVED = "-._~"


class ConnectLinkError(Exception):
    """The link is invalid. ``reason`` is a short code that is safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ConnectRequest:
    app_id: str
    external_user_id: str
    return_to: str
    exp: int


def _encode(value: str) -> str:
    return quote(value, safe=_UNRESERVED)


def canonical_string(params: Mapping[str, str]) -> str:
    return "&".join(
        f"{_encode(name)}={_encode(params[name])}" for name in sorted(params) if name != "sig"
    )


def sign(secret: str, params: Mapping[str, str]) -> str:
    message = canonical_string(params).encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def build_link(
    base_url: str,
    secret: str,
    *,
    app_id: str,
    external_user_id: str,
    return_to: str,
    lifetime_seconds: int = MAX_LIFETIME_SECONDS,
    now: int | None = None,
) -> str:
    """Return a signed connect URL. ``base_url`` is Token Keeper's ``/connect`` URL."""
    if not 0 < lifetime_seconds <= MAX_LIFETIME_SECONDS:
        raise ValueError("lifetime must be between 1 second and 10 minutes")
    current = int(time.time()) if now is None else now
    params = {
        "app_id": app_id,
        "external_user_id": external_user_id,
        "return_to": return_to,
        "exp": str(current + lifetime_seconds),
    }
    query = canonical_string(params) + "&sig=" + sign(secret, params)
    return f"{base_url}?{query}"


def verify_query(
    secret: str,
    raw_query: str,
    *,
    allowed_return_to: Mapping[str, Collection[str]],
    now: int | None = None,
) -> ConnectRequest:
    """Verify a raw query string and return the request it carries.

    ``allowed_return_to`` maps each known ``app_id`` to its exact ``return_to`` URLs.
    Raises ``ConnectLinkError``. Checks run signature-first, so nothing unsigned is trusted.
    """
    try:
        pairs = parse_qsl(raw_query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise ConnectLinkError("malformed_query") from None
    params: dict[str, str] = {}
    for name, value in pairs:
        if name in params:
            raise ConnectLinkError("duplicate_param")
        params[name] = value

    sig = params.pop("sig", None)
    if sig is None or set(params) != SIGNED_PARAMS:
        raise ConnectLinkError("bad_params")
    if not hmac.compare_digest(sig.encode(), sign(secret, params).encode()):
        raise ConnectLinkError("bad_signature")

    try:
        exp = int(params["exp"])
    except ValueError:
        raise ConnectLinkError("bad_exp") from None
    current = int(time.time()) if now is None else now
    if exp <= current:
        raise ConnectLinkError("expired")
    if exp - current > MAX_LIFETIME_SECONDS:
        raise ConnectLinkError("exp_too_far")

    app_id = params["app_id"]
    if app_id not in allowed_return_to:
        raise ConnectLinkError("unknown_app")
    return_to = params["return_to"]
    if return_to not in allowed_return_to[app_id] or "?" in return_to or "#" in return_to:
        raise ConnectLinkError("return_to_not_allowed")

    return ConnectRequest(
        app_id=app_id,
        external_user_id=params["external_user_id"],
        return_to=return_to,
        exp=exp,
    )
