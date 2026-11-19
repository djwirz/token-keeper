"""Recorded-shape GitHub responses for respx. Synthetic data, no real account.

Token strings are split so secret scanners don't flag them; they only need the real
prefixes, so the token-in-logs checks have something to find.
"""

from __future__ import annotations

from typing import Any

import httpx
import respx

from token_keeper import github

ACCESS_TOKEN = "ghu_" + "FakeAccessToken0000NotReal0000000000"
REFRESH_TOKEN = "ghr_" + "FakeRefreshToken000NotReal" + "0" * 50
SECOND_ACCESS_TOKEN = "ghu_" + "FakeAccessToken1111NotReal1111111111"


def token_body(access: str = ACCESS_TOKEN, refresh: str = REFRESH_TOKEN) -> dict[str, Any]:
    """The shape GitHub returns for a GitHub App with expiring user tokens."""
    return {
        "access_token": access,
        "expires_in": 28800,
        "refresh_token": refresh,
        "refresh_token_expires_in": 15811200,
        "scope": "",
        "token_type": "bearer",
    }


def user_body(
    user_id: int = 1234, login: str = "octo-demo", name: str = "Octo Demo"
) -> dict[str, Any]:
    return {
        "id": user_id,
        "login": login,
        "name": name,
        "avatar_url": f"https://avatars.githubusercontent.com/u/{user_id}?v=4",
        "public_repos": 8,
        "followers": 3,
        "type": "User",
    }


def mock_github(
    router: respx.MockRouter,
    *,
    token: dict[str, Any] | None = None,
    token_status: int = 200,
    user: dict[str, Any] | None = None,
    user_status: int = 200,
) -> tuple[respx.Route, respx.Route]:
    token_route = router.post(github.TOKEN_URL).mock(
        return_value=httpx.Response(token_status, json=token or token_body())
    )
    user_route = router.get(f"{github.API_URL}/user").mock(
        return_value=httpx.Response(user_status, json=user or user_body())
    )
    return token_route, user_route


ROTATED_ACCESS_TOKEN = "ghu_" + "FakeAccessToken2222NotReal2222222222"
ROTATED_REFRESH_TOKEN = "ghr_" + "FakeRefreshToken222NotReal" + "2" * 50


def events_body(login: str = "octo-demo", count: int = 12) -> list[dict[str, Any]]:
    """More than ten, so the worker's limit is exercised."""
    return [
        {
            "id": str(1000 + i),
            "type": "PushEvent" if i % 2 else "WatchEvent",
            "actor": {"login": login},
            "repo": {"id": i, "name": f"{login}/repo-{i}"},
            "payload": {},
            "public": True,
            "created_at": f"2026-11-18T{10 + i % 10:02d}:00:00Z",
        }
        for i in range(count)
    ]


def mock_events(
    router: respx.MockRouter, login: str = "octo-demo", status: int = 200
) -> respx.Route:
    return router.get(f"{github.API_URL}/users/{login}/events/public").mock(
        return_value=httpx.Response(status, json=events_body(login) if status == 200 else {})
    )
