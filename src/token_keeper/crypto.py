"""Field-level KMS encryption (ADR 0003).

Every call carries an encryption context that binds the ciphertext to its row:
``{"account_id": …}`` for tokens, ``{"state_hash": …}`` for the PKCE verifier. KMS
refuses to decrypt under any other context, so ciphertext can't be moved between rows.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import boto3

if TYPE_CHECKING:
    from mypy_boto3_kms import KMSClient


def state_hash(state: str) -> str:
    """SHA-256 of the OAuth state. Only this goes in keys and in CloudTrail."""
    return hashlib.sha256(state.encode()).hexdigest()


def account_context(account_id: str) -> dict[str, str]:
    return {"account_id": account_id}


def state_context(hashed_state: str) -> dict[str, str]:
    return {"state_hash": hashed_state}


class Crypto:
    def __init__(self, key_id: str, client: KMSClient | None = None) -> None:
        self._key_id = key_id
        self._client: KMSClient = client or boto3.client("kms")

    def encrypt(self, plaintext: str, context: dict[str, str]) -> bytes:
        response = self._client.encrypt(
            KeyId=self._key_id, Plaintext=plaintext.encode(), EncryptionContext=context
        )
        return response["CiphertextBlob"]

    def decrypt(self, ciphertext: bytes, context: dict[str, str]) -> str:
        response = self._client.decrypt(
            KeyId=self._key_id, CiphertextBlob=ciphertext, EncryptionContext=context
        )
        return response["Plaintext"].decode()
