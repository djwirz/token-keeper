"""GET /connect: signed link → state + PKCE → GitHub (spec §6.1, ADRs 0003 and 0007)."""

from __future__ import annotations

import base64
import hashlib

import pytest
from botocore.exceptions import ClientError

from tests.conftest import GITHUB_CLIENT_ID, TK_PUBLIC_URL, TK_TABLE, TokenKeeperEnv
from tests.tk_flow import connect_event, scan_table, signed_query, start_connect
from token_keeper import crypto, github
from token_keeper.handlers import connect

pytestmark = pytest.mark.usefixtures("tk_env")


def test_redirects_to_github_with_state_and_pkce() -> None:
    response, params = start_connect()
    location = response["headers"]["location"]
    assert location.startswith(github.AUTHORIZE_URL + "?")
    assert params["client_id"] == GITHUB_CLIENT_ID
    assert params["redirect_uri"] == f"{TK_PUBLIC_URL}/callback"
    assert params["code_challenge_method"] == "S256"
    # 32 random bytes, URL-safe base64 without padding.
    assert len(base64.urlsafe_b64decode(params["state"] + "=")) == 32
    assert response["headers"]["referrer-policy"] == "no-referrer"
    assert response["headers"]["cache-control"] == "no-store"


def test_state_is_stored_by_hash_with_an_encrypted_verifier(tk_env: TokenKeeperEnv) -> None:
    _, params = start_connect()
    (item,) = scan_table(TK_TABLE)
    hashed = crypto.state_hash(params["state"])
    assert item["PK"]["S"] == f"STATE#{hashed}"
    assert params["state"] not in str(item)

    kms = crypto.Crypto(tk_env.key_id)
    verifier = kms.decrypt(bytes(item["pkce_verifier_ct"]["B"]), crypto.state_context(hashed))
    assert verifier not in str(item)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    assert challenge.rstrip(b"=").decode() == params["code_challenge"]
    assert 43 <= len(verifier) <= 128  # RFC 7636

    # Bound to its row: KMS refuses it under any other state's hash.
    with pytest.raises(ClientError, match="InvalidCiphertext"):
        kms.decrypt(bytes(item["pkce_verifier_ct"]["B"]), crypto.state_context("0" * 64))


def test_each_connect_gets_a_fresh_state() -> None:
    _, first = start_connect()
    _, second = start_connect()
    assert first["state"] != second["state"]
    assert first["code_challenge"] != second["code_challenge"]


@pytest.mark.parametrize(
    "raw_query",
    [
        pytest.param(signed_query(secret="wrong-secret"), id="bad_signature"),
        pytest.param(signed_query(now=1_000_000), id="expired"),
        pytest.param(signed_query(app_id="other"), id="unknown_app"),
        pytest.param(signed_query(return_to="https://evil.example/"), id="return_to"),
        pytest.param("", id="empty"),
    ],
)
def test_rejections_render_the_error_page_and_store_nothing(raw_query: str) -> None:
    response = connect.handler(connect_event(raw_query), None)
    assert response["statusCode"] == 400
    assert "That didn't work" in response["body"]
    assert response["headers"]["referrer-policy"] == "no-referrer"
    assert scan_table(TK_TABLE) == []


def test_rejection_logs_a_reason_not_the_parameters(capsys: pytest.CaptureFixture[str]) -> None:
    raw = signed_query(external_user_id="u_secret_marker", secret="wrong-secret")
    connect.handler(connect_event(raw), None)
    out = capsys.readouterr().out
    assert "bad_signature" in out
    assert "u_secret_marker" not in out
