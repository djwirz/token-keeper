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
