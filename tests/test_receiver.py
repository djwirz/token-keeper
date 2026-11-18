from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode, urlsplit

import pytest

from receiver.handler import handler
from tests.conftest import CONNECT_SECRET, PASSCODE, PUBLIC_URL, WEBHOOK_SECRET, http_event
from tests.fixtures import sample_event
from token_keeper.contract import connect_links, webhooks

pytestmark = pytest.mark.usefixtures("receiver_env")


def _deliver(event: dict[str, Any], msg_id: str = "msg_1", secret: str = WEBHOOK_SECRET) -> int:
    body = json.dumps(event)
    headers = webhooks.build_headers([secret], msg_id, body.encode())
    response = handler(
        http_event("POST", "/webhooks/token-keeper", body=body, headers=headers), None
    )
    return int(response["statusCode"])


def _page(query: dict[str, str] | None = None) -> str:
    response = handler(http_event("GET", "/", query=query), None)
    assert response["statusCode"] == 200
    assert "content-security-policy" in response["headers"]
    return str(response["body"])


def test_empty_page() -> None:
    assert "No accounts connected yet." in _page()


def test_valid_event_is_stored_and_rendered() -> None:
    assert _deliver(sample_event()) == 204
    page = _page()
    assert "@octo-demo" in page
    assert "Connected" in page
    assert "PushEvent on octo-demo/hello" in page


def test_duplicate_delivery_is_a_no_op() -> None:
    assert _deliver(sample_event(version=2, name="First")) == 204
    assert _deliver(sample_event(version=3, name="Second"), msg_id="msg_1") == 204
    page = _page()
    assert "First" in page and "Second" not in page


def test_lower_version_is_ignored() -> None:
    assert _deliver(sample_event(version=5, name="Newer"), msg_id="msg_a") == 204
    assert _deliver(sample_event(version=4, name="Older"), msg_id="msg_b") == 204
    page = _page()
    assert "Newer" in page and "Older" not in page and "Version 5" in page


def test_needs_reauth_is_shown() -> None:
    _deliver(sample_event(version=1), msg_id="msg_a")
    _deliver(sample_event(version=2, state="needs_reauth", state_reason="revoked"), msg_id="msg_b")
    assert "Needs re-auth (revoked)" in _page()


def test_bad_signature_is_rejected_and_nothing_stored() -> None:
    other = "whsec_" + "b3RoZXItc2VjcmV0LW5vdC1yZWFsLWF0LWFsbA=="
    assert _deliver(sample_event(), secret=other) == 401
    assert "No accounts connected yet." in _page()


def test_signed_but_invalid_payload_is_400() -> None:
    bad = sample_event()
    bad["state"] = "refresh_failing"
    assert _deliver(bad) == 400


def test_github_data_is_escaped() -> None:
    _deliver(sample_event(name="<script>alert(1)</script>"))
    page = _page()
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_result_banner() -> None:
    assert "GitHub account connected." in _page({"result": "connected"})
    assert "already linked" in _page({"result": "already_linked"})
    assert "banner" not in _page({"result": "<b>made-up</b>"}).split("<h1>")[1].split("<form")[0]


def _connect(passcode: str) -> dict[str, Any]:
    body = urlencode({"passcode": passcode})
    return handler(http_event("POST", "/connect", body=body), None)


def test_wrong_passcode_is_refused() -> None:
    response = _connect("nope")
    assert response["statusCode"] == 403
    assert "location" not in response["headers"]
    assert "Wrong passcode." in response["body"]


def test_right_passcode_redirects_to_a_verifiable_link() -> None:
    response = _connect(PASSCODE)
    assert response["statusCode"] == 303
    link = urlsplit(response["headers"]["location"])
    assert f"{link.scheme}://{link.netloc}{link.path}" == "http://localhost:8000/connect"
    request = connect_links.verify_query(
        CONNECT_SECRET, link.query, allowed_return_to={"demo": [PUBLIC_URL]}
    )
    assert request.external_user_id == "u_demo"


def test_unknown_route_is_404() -> None:
    assert handler(http_event("GET", "/nope"), None)["statusCode"] == 404
    assert handler(http_event("GET", "/connect"), None)["statusCode"] == 404
