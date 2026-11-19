"""The account event payload sent to the main app (spec §6.3, ADRs 0005 and 0011).

Unknown fields are ignored on parse so the contract can grow additively.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, cast, get_args

EventType = Literal["account.connected", "account.updated"]
State = Literal["active", "needs_reauth"]
StateReason = Literal["revoked", "refresh_rejected", "refresh_expired", "unauthorized"]

EVENT_TYPES: frozenset[str] = frozenset(get_args(EventType))
STATES: frozenset[str] = frozenset(get_args(State))
STATE_REASONS: frozenset[str] = frozenset(get_args(StateReason))


class EventParseError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class GitHubIdentity:
    user_id: int
    login: str


@dataclass(frozen=True)
class Profile:
    name: str | None
    avatar_url: str | None
    public_repos: int
    followers: int


@dataclass(frozen=True)
class RecentEvent:
    type: str
    repo: str
    created_at: str


@dataclass(frozen=True)
class Snapshot:
    fetched_at: str
    profile: Profile
    recent_events: list[RecentEvent] = field(default_factory=list)


@dataclass(frozen=True)
class AccountEvent:
    type: EventType
    app_id: str
    account_id: str
    external_user_id: str
    version: int
    state: State
    state_reason: StateReason | None
    provider: str
    github: GitHubIdentity
    snapshot: Snapshot | None
    occurred_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _req(obj: dict[str, Any], key: str, kind: type | tuple[type, ...]) -> Any:
    if key not in obj:
        raise EventParseError(f"missing:{key}")
    value = obj[key]
    # bool is an int subclass; never accept it where a number is expected.
    if not isinstance(value, kind) or (isinstance(value, bool) and kind is int):
        raise EventParseError(f"type:{key}")
    return value


def _opt_str(obj: dict[str, Any], key: str) -> str | None:
    value = obj.get(key)
    if value is not None and not isinstance(value, str):
        raise EventParseError(f"type:{key}")
    return value


def parse_event(data: Any) -> AccountEvent:
    """Validate a decoded JSON payload. Raises ``EventParseError``."""
    if not isinstance(data, dict):
        raise EventParseError("not_object")

    event_type = _req(data, "type", str)
    if event_type not in EVENT_TYPES:
        raise EventParseError("unknown_type")
    state = _req(data, "state", str)
    if state not in STATES:
        raise EventParseError("unknown_state")
    reason = _opt_str(data, "state_reason")
    if reason is not None and reason not in STATE_REASONS:
        raise EventParseError("unknown_state_reason")
    if (state == "active") != (reason is None):
        raise EventParseError("state_reason_mismatch")

    gh = _req(data, "github", dict)
    github = GitHubIdentity(user_id=_req(gh, "user_id", int), login=_req(gh, "login", str))

    snapshot: Snapshot | None = None
    raw_snapshot = data.get("snapshot")
    if raw_snapshot is not None:
        if not isinstance(raw_snapshot, dict):
            raise EventParseError("type:snapshot")
        prof = _req(raw_snapshot, "profile", dict)
        events = _req(raw_snapshot, "recent_events", list)
        snapshot = Snapshot(
            fetched_at=_req(raw_snapshot, "fetched_at", str),
            profile=Profile(
                name=_opt_str(prof, "name"),
                avatar_url=_opt_str(prof, "avatar_url"),
                public_repos=_req(prof, "public_repos", int),
                followers=_req(prof, "followers", int),
            ),
            recent_events=[
                RecentEvent(
                    type=_req(e, "type", str),
                    repo=_req(e, "repo", str),
                    created_at=_req(e, "created_at", str),
                )
                for e in (_require_dict(x) for x in events)
            ],
        )

    version = _req(data, "version", int)
    if version < 1:
        raise EventParseError("bad_version")

    return AccountEvent(
        type=cast(EventType, event_type),
        app_id=_req(data, "app_id", str),
        account_id=_req(data, "account_id", str),
        external_user_id=_req(data, "external_user_id", str),
        version=version,
        state=cast(State, state),
        state_reason=cast(StateReason | None, reason),
        provider=_req(data, "provider", str),
        github=github,
        snapshot=snapshot,
        occurred_at=_req(data, "occurred_at", str),
    )


def _require_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EventParseError("type:recent_events")
    return value
