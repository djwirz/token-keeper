"""Clients built once per Lambda container (cold start), shared by the handlers."""

from __future__ import annotations

import time
from functools import cache

from token_keeper import settings
from token_keeper.crypto import Crypto
from token_keeper.store import Store


def now() -> int:
    return int(time.time())


@cache
def store() -> Store:
    return Store(settings.get().table_name)


@cache
def crypto() -> Crypto:
    return Crypto(settings.get().kms_key_id)


def reset() -> None:
    """Forget cached config and clients (tests and the local runner)."""
    settings.get.cache_clear()
    store.cache_clear()
    crypto.cache_clear()
