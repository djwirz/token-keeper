from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from standardwebhooks.webhooks import Webhook

from token_keeper.contract import webhooks
from token_keeper.contract.webhooks import WebhookVerificationError

VECTORS: dict[str, Any] = json.loads(
    (Path(__file__).parent / "vectors" / "standard_webhooks.json").read_text()
)
SECRET = VECTORS["sign"][0]["secret"]
MSG_ID = "msg_test"
BODY = b'{"hello": "world"}'
NOW = 1_700_000_000


def _payload(case: dict[str, Any]) -> bytes:
    if "payload_bytes_range" in case:
        return bytes(range(case["payload_bytes_range"]))
    return str(case["payload_utf8"]).encode()


@pytest.mark.parametrize("case", VECTORS["sign"])
def test_sign_matches_published_vectors(case: dict[str, Any]) -> None:
    sig = webhooks.sign(case["secret"], case["msg_id"], case["timestamp"], _payload(case))
    assert sig == case["expected"]


@pytest.mark.parametrize("case", VECTORS["sign"])
def test_verify_accepts_published_vectors(case: dict[str, Any]) -> None:
    headers = {
        "webhook-id": case["msg_id"],
        "webhook-timestamp": str(case["timestamp"]),
        "webhook-signature": case["expected"],
    }
    assert (
        webhooks.verify(case["secret"], headers, _payload(case), now=case["timestamp"])
        == (case["msg_id"])
    )


def test_published_invalid_signature_is_rejected() -> None:
    case = VECTORS["sign"][0]
    headers = {
        "webhook-id": case["msg_id"],
        "webhook-timestamp": str(case["timestamp"]),
        "webhook-signature": VECTORS["invalid_signature"],
    }
    with pytest.raises(WebhookVerificationError, match="bad_signature"):
        webhooks.verify(SECRET, headers, _payload(case), now=case["timestamp"])


def test_reference_library_verifies_our_signature() -> None:
    headers = webhooks.build_headers([SECRET], MSG_ID, BODY, timestamp=NOW)
    # The reference library checks against the real clock, so sign with "now".
    live = webhooks.build_headers([SECRET], MSG_ID, BODY)
    assert Webhook(SECRET).verify(BODY, live) == {"hello": "world"}
    assert headers["webhook-signature"].startswith("v1,")


def test_we_verify_reference_library_signature() -> None:
    from datetime import UTC, datetime

    sig = Webhook(SECRET).sign(MSG_ID, datetime.fromtimestamp(NOW, tz=UTC), BODY.decode())
    headers = {"webhook-id": MSG_ID, "webhook-timestamp": str(NOW), "webhook-signature": sig}
    assert webhooks.verify(SECRET, headers, BODY, now=NOW) == MSG_ID


def _headers(**overrides: str) -> dict[str, str]:
    headers = webhooks.build_headers([SECRET], MSG_ID, BODY, timestamp=NOW)
    headers.update(overrides)
    return headers


def test_round_trip_and_case_insensitive_headers() -> None:
    upper = {k.upper(): v for k, v in _headers().items()}
    assert webhooks.verify(SECRET, upper, BODY, now=NOW) == MSG_ID


@pytest.mark.parametrize(
    ("headers", "body", "reason"),
    [
        (_headers(), b'{"hello": "worle"}', "bad_signature"),
        (_headers(**{"webhook-id": "msg_other"}), BODY, "bad_signature"),
        (_headers(**{"webhook-timestamp": str(NOW + 1)}), BODY, "bad_signature"),
        (_headers(**{"webhook-timestamp": "soon"}), BODY, "bad_timestamp"),
        ({"webhook-id": MSG_ID, "webhook-timestamp": str(NOW)}, BODY, "missing_header"),
    ],
)
def test_tampering_is_rejected(headers: dict[str, str], body: bytes, reason: str) -> None:
    with pytest.raises(WebhookVerificationError) as err:
        webhooks.verify(SECRET, headers, body, now=NOW)
    assert err.value.reason == reason


@pytest.mark.parametrize("skew", [-301, 301])
def test_stale_or_future_timestamp_is_rejected(skew: int) -> None:
    with pytest.raises(WebhookVerificationError, match="timestamp_out_of_range"):
        webhooks.verify(SECRET, _headers(), BODY, now=NOW + skew)


@pytest.mark.parametrize("skew", [-300, 300])
def test_timestamp_at_tolerance_edge_is_accepted(skew: int) -> None:
    assert webhooks.verify(SECRET, _headers(), BODY, now=NOW + skew) == MSG_ID


def test_rotation_signs_with_every_secret_and_either_verifies() -> None:
    new_secret = "whsec_" + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0NTY3ODk="
    headers = webhooks.build_headers([SECRET, new_secret], MSG_ID, BODY, timestamp=NOW)
    assert len(headers["webhook-signature"].split(" ")) == 2
    assert webhooks.verify(SECRET, headers, BODY, now=NOW) == MSG_ID
    assert webhooks.verify(new_secret, headers, BODY, now=NOW) == MSG_ID


def test_unknown_versions_and_junk_are_skipped() -> None:
    good = _headers()["webhook-signature"]
    junk = f"v2,{good[3:]} nonsense v1, {good}"
    assert webhooks.verify(SECRET, _headers(**{"webhook-signature": junk}), BODY, now=NOW)
    with pytest.raises(WebhookVerificationError):
        webhooks.verify(SECRET, _headers(**{"webhook-signature": f"v2,{good[3:]}"}), BODY, now=NOW)


def test_check_secret_accepts_real_secrets_and_rejects_bad_ones() -> None:
    webhooks.check_secret(SECRET)  # the published test key: 24 bytes
    with pytest.raises(ValueError, match="base64"):
        webhooks.check_secret("whsec_not base64!")
    for too_short in ("whsec_", "whsec_dG9vLXNob3J0"):
        with pytest.raises(ValueError, match="at least 24 bytes"):
            webhooks.check_secret(too_short)
