"""Identifiers: a prefix plus a ULID (time-ordered, 26 Crockford base32 characters).

``acc_…`` accounts, ``msg_…`` webhook ids (spec §6.3), ``cor_…`` correlation ids (§8).
"""

from __future__ import annotations

import secrets
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid(now_ms: int | None = None) -> str:
    """48 bits of millisecond time, then 80 random bits."""
    ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    value = (ms << 80) | int.from_bytes(secrets.token_bytes(10), "big")
    return "".join(_ALPHABET[(value >> (5 * i)) & 31] for i in reversed(range(26)))


def new(prefix: str) -> str:
    return f"{prefix}_{ulid()}"
