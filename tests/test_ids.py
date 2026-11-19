from __future__ import annotations

import re

from token_keeper import ids


def test_ulid_shape_and_order() -> None:
    first = ids.ulid(now_ms=1_700_000_000_000)
    second = ids.ulid(now_ms=1_700_000_000_001)
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", first)
    assert first < second
    assert ids.ulid(now_ms=0).startswith("0000000000")


def test_prefixed_ids_are_unique() -> None:
    made = {ids.new("acc") for _ in range(1000)}
    assert len(made) == 1000
    assert all(m.startswith("acc_") for m in made)
