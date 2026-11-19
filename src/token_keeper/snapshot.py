"""The public snapshot stored per account and sent in every event (spec §6.3).

Profile fields and the last few public events, overwritten on each sync rather than
accumulated (spec §9).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from token_keeper.github import GitHubUser


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build(
    user: GitHubUser, fetched_at: int, recent_events: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    """At connect time ``recent_events`` is empty; a sync fills it."""
    return {
        "fetched_at": iso(fetched_at),
        "profile": {
            "name": user.name,
            "avatar_url": user.avatar_url,
            "public_repos": user.public_repos,
            "followers": user.followers,
        },
        "recent_events": recent_events or [],
    }
