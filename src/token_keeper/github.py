"""The GitHub calls this slice needs (spec §5): authorize, code exchange, ``GET /user``.

Token-endpoint calls send ``Accept: application/json`` and treat an ``error`` field in
the body as the failure, whatever the HTTP status: GitHub answers most token errors
with a 200.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from typing import Any
from urllib.parse import urlencode

import httpx

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105 (a URL)
API_URL = "https://api.github.com"
TIMEOUT_SECONDS = 5.0
_SAFE_CODE = re.compile(r"^[a-z_]{1,64}$")


class GitHubError(Exception):
    """A GitHub call failed. ``reason`` is a short code that is safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
        raise GitHubError(f"token_error:{safe_code(body['error'])}")
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
    try:
        response = _client().post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
            headers={"accept": "application/json"},
        )
        body = response.json()
    except httpx.HTTPError:
        raise GitHubError("token_unreachable") from None
    except ValueError:
        raise GitHubError(f"token_http_{response.status_code}") from None
    if not isinstance(body, dict):
        raise GitHubError("token_response_malformed")
    return _token_pair(body, now)


def get_user(access_token: str) -> GitHubUser:
    try:
        response = _client().get(
            f"{API_URL}/user",
            headers={
                "authorization": f"Bearer {access_token}",
                "accept": "application/vnd.github+json",
                "x-github-api-version": "2022-11-28",
            },
        )
    except httpx.HTTPError:
        raise GitHubError("user_unreachable") from None
    if response.status_code != 200:
        raise GitHubError(f"user_http_{response.status_code}")
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
