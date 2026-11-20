"""The GitHub calls Token Keeper makes (spec §5): authorize, code exchange, refresh,
``GET /user`` and ``GET /users/{login}/events/public``, plus the signature check on
GitHub's App webhook deliveries (spec §6.4).

Token-endpoint calls send ``Accept: application/json`` and treat an ``error`` field in
the body as the failure, whatever the HTTP status: GitHub answers most token errors
with a 200.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from functools import cache
from typing import Any
from urllib.parse import quote, urlencode

import httpx

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105 (a URL)
API_URL = "https://api.github.com"
TIMEOUT_SECONDS = 5.0
_SAFE_CODE = re.compile(r"^[a-z_]{1,64}$")


class GitHubError(Exception):
    """A GitHub call failed. ``reason`` is a short code that is safe to log.

    The base class is transient (5xx, timeouts, odd responses): it never changes an
    account's state (spec §7). The subclasses below are the ones that can.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RefreshRejected(GitHubError):
    """``bad_refresh_token``: the refresh token is dead (used, revoked or expired)."""


class Unauthorized(GitHubError):
    """A 401 from the API: the access token no longer works."""


class RateLimited(GitHubError):
    """A 403 or 429 with rate-limit headers. ``reset_at`` is when to try again."""

    def __init__(self, reason: str, reset_at: int) -> None:
        super().__init__(reason)
        self.reset_at = reset_at


SIGNATURE_PREFIX = "sha256="


class WebhookSignatureError(Exception):
    """An App webhook delivery isn't signed with our secret. ``reason`` is safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def verify_webhook_signature(secret: str, header: str | None, body: bytes) -> None:
    """Check ``X-Hub-Signature-256`` (spec §6.4) before anything reads the body.

    GitHub sends ``sha256=`` and the lowercase hex HMAC-SHA256 of the raw body, keyed
    with the App's webhook secret. Compared in constant time, as GitHub's docs ask.
    Only the body is signed: the headers (the event name, the delivery GUID) are not.
    """
    if not header:
        raise WebhookSignatureError("missing_signature")
    expected = SIGNATURE_PREFIX + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(header.encode(), expected.encode()):
        raise WebhookSignatureError("bad_signature")


def safe_code(value: object) -> str:
    """GitHub's own error code if it looks like one, so nothing odd reaches a log line."""
    return value if isinstance(value, str) and _SAFE_CODE.match(value) else "unrecognised"


@dataclass(frozen=True, repr=False)
class TokenPair:
    access_token: str
    refresh_token: str
    access_expires_at: int
    refresh_expires_at: int

    def __repr__(self) -> str:
        return "TokenPair(<redacted>)"


@dataclass(frozen=True)
class GitHubUser:
    id: int
    login: str
    name: str | None
    avatar_url: str | None
    public_repos: int
    followers: int


@cache
def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_SECONDS, headers={"user-agent": "token-keeper"})


def authorize_url(client_id: str, redirect_uri: str, state: str, code_challenge: str) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _token_pair(body: dict[str, Any], now: int) -> TokenPair:
    if "error" in body:
        code = safe_code(body["error"])
        if code == "bad_refresh_token":
            raise RefreshRejected(f"token_error:{code}")
        raise GitHubError(f"token_error:{code}")
    try:
        access = body["access_token"]
        refresh = body.get("refresh_token")
        if refresh is None:
            # The App has "expire user authorization tokens" off (spec §5 needs it on).
            raise GitHubError("token_not_expiring")
        return TokenPair(
            access_token=str(access),
            refresh_token=str(refresh),
            access_expires_at=now + int(body["expires_in"]),
            refresh_expires_at=now + int(body["refresh_token_expires_in"]),
        )
    except (KeyError, TypeError, ValueError):
        raise GitHubError("token_response_malformed") from None


def exchange_code(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    now: int,
) -> TokenPair:
    return _token_call(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        now,
    )


def refresh(*, client_id: str, client_secret: str, refresh_token: str, now: int) -> TokenPair:
    """Rotate the pair. Single-use: after this, the old refresh and access tokens are dead.

    A timeout here may mean GitHub rotated and we never saw the answer: the lost
    rotation residual risk in spec §7.
    """
    return _token_call(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        now,
    )


def _token_call(data: dict[str, str], now: int) -> TokenPair:
    try:
        response = _client().post(TOKEN_URL, data=data, headers={"accept": "application/json"})
        body = response.json()
    except httpx.HTTPError:
        raise GitHubError("token_unreachable") from None
    except ValueError:
        raise GitHubError(f"token_http_{response.status_code}") from None
    if not isinstance(body, dict):
        raise GitHubError("token_response_malformed")
    return _token_pair(body, now)


def _rate_limit_reset(response: httpx.Response, now: int) -> int | None:
    """When to try again, if this 403/429 is a rate limit; None if it isn't one."""
    if response.status_code not in (403, 429):
        return None
    retry_after = response.headers.get("retry-after")
    if retry_after and retry_after.isdigit():
        return now + int(retry_after)
    reset = response.headers.get("x-ratelimit-reset")
    if response.headers.get("x-ratelimit-remaining") == "0" and reset and reset.isdigit():
        return int(reset)
    return None


def _api_get(
    path: str, access_token: str, name: str, now: int, params: dict[str, Any] | None = None
) -> httpx.Response:
    try:
        response = _client().get(
            f"{API_URL}{path}",
            params=params,
            headers={
                "authorization": f"Bearer {access_token}",
                "accept": "application/vnd.github+json",
                "x-github-api-version": "2022-11-28",
            },
        )
    except httpx.HTTPError:
        raise GitHubError(f"{name}_unreachable") from None
    if response.status_code == 200:
        return response
    if response.status_code == 401:
        raise Unauthorized(f"{name}_http_401")
    reset_at = _rate_limit_reset(response, now)
    if reset_at is not None:
        raise RateLimited(f"{name}_rate_limited", reset_at)
    raise GitHubError(f"{name}_http_{response.status_code}")


def get_user(access_token: str, now: int = 0) -> GitHubUser:
    response = _api_get("/user", access_token, "user", now)
    try:
        body = response.json()
        return GitHubUser(
            id=int(body["id"]),
            login=str(body["login"]),
            name=body.get("name"),
            avatar_url=body.get("avatar_url"),
            public_repos=int(body.get("public_repos") or 0),
            followers=int(body.get("followers") or 0),
        )
    except (KeyError, TypeError, ValueError):
        raise GitHubError("user_response_malformed") from None


def get_public_events(
    login: str, access_token: str, now: int = 0, limit: int = 10
) -> list[dict[str, str]]:
    """The user's last few public events (spec §5): public only, nothing private.

    Newest first by ``created_at``: GitHub's feed is close to that order but not
    exactly (the slice 3 browser check showed a push listed above a later one).
    """
    response = _api_get(
        f"/users/{quote(login, safe='')}/events/public",
        access_token,
        "events",
        now,
        params={"per_page": limit},
    )
    try:
        body = response.json()
        events = [
            {
                "type": str(event["type"]),
                "repo": str(event["repo"]["name"]),
                "created_at": str(event["created_at"]),
            }
            for event in body[:limit]
        ]
        # ISO 8601 UTC strings sort in time order.
        return sorted(events, key=lambda e: e["created_at"], reverse=True)
    except (KeyError, TypeError, ValueError):
        raise GitHubError("events_response_malformed") from None
