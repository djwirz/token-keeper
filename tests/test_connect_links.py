from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from token_keeper.contract import connect_links
from token_keeper.contract.connect_links import ConnectLinkError

VECTORS: dict[str, Any] = json.loads(
    (Path(__file__).parent / "vectors" / "connect_links.json").read_text()
)
SECRET = VECTORS["secret"]
NOW = 1_700_000_000
RETURN_TO = "http://localhost:8000/"
ALLOWED = {"demo": [RETURN_TO]}


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["name"])
def test_canonical_string_matches_independent_vectors(case: dict[str, Any]) -> None:
    assert connect_links.canonical_string(case["params"]) == case["canonical"]


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["name"])
def test_signature_matches_openssl(case: dict[str, Any]) -> None:
    assert connect_links.sign(SECRET, case["params"]) == case["sig"]


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["name"])
def test_vector_query_verifies(case: dict[str, Any]) -> None:
    allowed = {case["params"]["app_id"]: [case["params"]["return_to"]]}
    query = f"{case['canonical']}&sig={case['sig']}"
    request = connect_links.verify_query(SECRET, query, allowed_return_to=allowed, now=NOW)
    assert request.external_user_id == case["params"]["external_user_id"]


def _link(**kwargs: Any) -> str:
    args: dict[str, Any] = {
        "app_id": "demo",
        "external_user_id": "u_demo",
        "return_to": RETURN_TO,
        "now": NOW,
    }
    args.update(kwargs)
    return connect_links.build_link("http://localhost:8000/connect", SECRET, **args)


def _query(link: str) -> str:
    return urlsplit(link).query


def test_build_then_verify_round_trip() -> None:
    request = connect_links.verify_query(
        SECRET, _query(_link()), allowed_return_to=ALLOWED, now=NOW
    )
    assert request == connect_links.ConnectRequest("demo", "u_demo", RETURN_TO, NOW + 600)


def _reason(query: str, now: int = NOW, allowed: dict[str, list[str]] | None = None) -> str:
    with pytest.raises(ConnectLinkError) as err:
        connect_links.verify_query(
            SECRET, query, allowed_return_to=ALLOWED if allowed is None else allowed, now=now
        )
    return err.value.reason


def test_wrong_secret() -> None:
    link = connect_links.build_link(
        "http://x/connect",
        "other",
        app_id="demo",
        external_user_id="u_demo",
        return_to=RETURN_TO,
        now=NOW,
    )
    assert _reason(_query(link)) == "bad_signature"
    assert _reason(_query(_link()).replace("u_demo", "u_evil")) == "bad_signature"


def test_expiry_rules() -> None:
    assert _reason(_query(_link()), now=NOW + 600) == "expired"
    assert _reason(_query(_link()), now=NOW - 1) == "exp_too_far"
    with pytest.raises(ValueError):
        _link(lifetime_seconds=601)


def test_param_rules() -> None:
    query = _query(_link())
    assert _reason(query + "&extra=1") == "bad_params"
    assert _reason(query + "&app_id=demo") == "duplicate_param"
    assert _reason(query.split("&sig=")[0]) == "bad_params"
    assert _reason("a=1&&") == "malformed_query"


def test_unknown_app_and_return_to() -> None:
    assert _reason(_query(_link(app_id="other"))) == "unknown_app"
    assert _reason(_query(_link(return_to="http://localhost:8000/evil"))) == (
        "return_to_not_allowed"
    )
    # A query on return_to is rejected even if someone put it on the allow-list.
    with_query = "http://localhost:8000/?next=x"
    assert (
        _reason(_query(_link(return_to=with_query)), allowed={"demo": [with_query]})
        == "return_to_not_allowed"
    )
