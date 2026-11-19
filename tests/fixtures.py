"""Synthetic test data. No real GitHub accounts."""

from __future__ import annotations

from typing import Any


def sample_event(
    *,
    version: int = 1,
    state: str = "active",
    state_reason: str | None = None,
    name: str | None = "Octo Demo",
    event_type: str = "account.updated",
) -> dict[str, Any]:
    return {
        "type": event_type,
        "app_id": "demo",
        "account_id": "acc_01JTEST",
        "external_user_id": "u_demo",
        "version": version,
        "state": state,
        "state_reason": state_reason,
        "provider": "github",
        "github": {"user_id": 1234, "login": "octo-demo"},
        "snapshot": {
            "fetched_at": "2026-11-18T20:00:00Z",
            "profile": {
                "name": name,
                "avatar_url": "https://avatars.example.test/u/1234",
                "public_repos": 8,
                "followers": 3,
            },
            "recent_events": [
                {
                    "type": "PushEvent",
                    "repo": "octo-demo/hello",
                    "created_at": "2026-11-18T19:00:00Z",
                }
            ],
        },
        "occurred_at": "2026-11-18T20:00:01Z",
    }
