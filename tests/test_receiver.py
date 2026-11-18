from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode, urlsplit

import pytest

from receiver import config
from receiver.handler import handler
from receiver.store import Store
from tests.conftest import (
    CONNECT_SECRET,
    CONNECT_URL,
    PASSCODE,
    PUBLIC_URL,
    TABLE,
    WEBHOOK_SECRET,
    http_event,
)
from tests.fixtures import sample_event
from token_keeper.contract import connect_links, webhooks
from token_keeper.contract.events import parse_event

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


def test_redelivery_is_a_no_op() -> None:
    assert _deliver(sample_event(version=2, name="First")) == 204
    assert _deliver(sample_event(version=2, name="Again"), msg_id="msg_2") == 204
    page = _page()
    assert "First" in page and "Again" not in page


def test_retry_with_newer_state_is_stored() -> None:
    # Token Keeper keeps webhook-id across retries but rebuilds the body each attempt,
    # so a retry can carry a higher version. Version decides, not the id (ADR 0012).
    assert _deliver(sample_event(version=2, name="First"), msg_id="msg_1") == 204
    assert _deliver(sample_event(version=3, name="Newer"), msg_id="msg_1") == 204
    page = _page()
    assert "Newer" in page and "Version 3" in page


def test_store_reports_what_it_did() -> None:
    store = Store(TABLE)
    assert store.save_event(parse_event(sample_event(version=2))) == "stored"
    assert store.save_event(parse_event(sample_event(version=2))) == "stale"
    assert store.save_event(parse_event(sample_event(version=1))) == "stale"
    assert store.save_event(parse_event(sample_event(version=3))) == "stored"


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
    assert f"{link.scheme}://{link.netloc}{link.path}" == CONNECT_URL
    request = connect_links.verify_query(
        CONNECT_SECRET, link.query, allowed_return_to={"demo": [PUBLIC_URL]}
    )
    assert request.external_user_id == "u_demo"


def test_page_csp_allows_the_connect_redirect() -> None:
    # Browsers apply form-action to the redirect after a form submit, so the page's CSP
    # must allow the origin the passcode form redirects to, or Connect silently fails.
    page = handler(http_event("GET", "/"), None)
    location = urlsplit(_connect(PASSCODE)["headers"]["location"])
    form_action = next(
        d
        for d in page["headers"]["content-security-policy"].split("; ")
        if d.startswith("form-action ")
    )
    assert f"{location.scheme}://{location.netloc}" in form_action.split()
    assert "'self'" in form_action.split()


def test_unreadable_body_is_400_not_500() -> None:
    event = http_event("POST", "/webhooks/token-keeper", body="not base64!")
    event["isBase64Encoded"] = True
    assert handler(event, None)["statusCode"] == 400


def test_malformed_webhook_secret_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", "whsec_dG9vLXNob3J0")  # decodes to 9 bytes
    config.load.cache_clear()
    with pytest.raises(ValueError, match="at least 24 bytes"):
        config.load()
    config.load.cache_clear()


def test_unknown_route_is_404() -> None:
    assert handler(http_event("GET", "/nope"), None)["statusCode"] == 404
    assert handler(http_event("GET", "/connect"), None)["statusCode"] == 404
