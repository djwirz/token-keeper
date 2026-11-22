"""Clients built once per Lambda container (cold start), shared by the handlers."""

from __future__ import annotations

import time
from functools import cache

from token_keeper import settings, sync
from token_keeper.crypto import Crypto, account_context
from token_keeper.github import TokenPair
from token_keeper.store import EncryptedTokens, Store


def now() -> int:
    return int(time.time())


@cache
def store() -> Store:
    return Store(settings.get().table_name)


@cache
def crypto() -> Crypto:
    return Crypto(settings.get().kms_key_id)


def encrypt_tokens(account_id: str, tokens: TokenPair) -> EncryptedTokens:
    """Encrypt a pair under the ``account_id`` context (ADR 0003) before it's written."""
    context = account_context(account_id)
    return EncryptedTokens(
        access_token_ct=crypto().encrypt(tokens.access_token, context),
        refresh_token_ct=crypto().encrypt(tokens.refresh_token, context),
        access_expires_at=tokens.access_expires_at,
        refresh_expires_at=tokens.refresh_expires_at,
    )


def reset() -> None:
    """Forget cached config and clients (tests and the local runner)."""
    settings.get.cache_clear()
    sync.reset_interval_cache()
    store.cache_clear()
    crypto.cache_clear()
