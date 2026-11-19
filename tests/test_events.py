from __future__ import annotations

import copy
from typing import Any

import pytest

from tests.fixtures import sample_event
from token_keeper.contract.events import EventParseError, parse_event


def test_parse_round_trip() -> None:
    data = sample_event()
    event = parse_event(data)
    assert event.github.login == "octo-demo"
    assert event.snapshot is not None
    assert event.snapshot.recent_events[0].type == "PushEvent"
    assert event.to_dict() == data


def test_unknown_fields_are_ignored() -> None:
    data = sample_event()
    data["future_field"] = {"anything": 1}
    assert parse_event(data).version == data["version"]


def test_snapshot_may_be_absent() -> None:
    data = sample_event()
    data["snapshot"] = None
    assert parse_event(data).snapshot is None


def _mutated(path: list[str], value: Any) -> dict[str, Any]:
    data = copy.deepcopy(sample_event())
    target = data
    for key in path[:-1]:
        target = target[key]
    if value is KeyError:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return data


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        (["type"], "account.deleted", "unknown_type"),
        (["state"], "refresh_failing", "unknown_state"),
        (["state_reason"], "revoked", "state_reason_mismatch"),
        (["version"], True, "type:version"),
        (["version"], 0, "bad_version"),
        (["account_id"], KeyError, "missing:account_id"),
        (["github", "user_id"], "1234", "type:user_id"),
        (["snapshot", "recent_events"], [1], "type:recent_events"),
    ],
)
def test_invalid_payloads(path: list[str], value: Any, reason: str) -> None:
    with pytest.raises(EventParseError) as err:
        parse_event(_mutated(path, value))
    assert err.value.reason == reason


def test_needs_reauth_requires_reason() -> None:
    data = sample_event(state="needs_reauth", state_reason="revoked")
    assert parse_event(data).state_reason == "revoked"
    data["state_reason"] = None
    with pytest.raises(EventParseError, match="state_reason_mismatch"):
        parse_event(data)
