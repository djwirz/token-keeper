"""Smoke test: the real local runner, over HTTP, against a moto server."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from local import run
from local.send_event import send, synthetic_event
from receiver import config, handler
from tests.conftest import GITHUB_WEBHOOK_SECRET
from tests.github_fakes import (
    ROTATED_ACCESS_TOKEN,
    ROTATED_REFRESH_TOKEN,
    mock_events,
    mock_github,
    token_body,
)
from tests.tk_flow import github_delivery
from token_keeper.handlers import _shared, deliverer


def _clear_caches() -> None:
    config.load.cache_clear()
    handler._store.cache_clear()
    _shared.reset()
    deliverer._sqs.cache_clear()


def _start(env_file: Path) -> Iterator[run.Running]:
    saved = dict(os.environ)
    _clear_caches()
    running = run.start(
        receiver_port=run.free_port(),
        env_file=env_file,
        token_keeper_port=run.free_port(),
        control_port=run.free_port(),
    )
    try:
        yield running
    finally:
        running.stop()
        os.environ.clear()
        os.environ.update(saved)
        _clear_caches()


@pytest.fixture
def runner(tmp_path: Path) -> Iterator[run.Running]:
    yield from _start(tmp_path / ".env.local")


@pytest.fixture
def runner_with_github(tmp_path: Path) -> Iterator[run.Running]:
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "GITHUB_CLIENT_ID=Iv23-test\nGITHUB_CLIENT_SECRET=test-not-real\n"
        f"GITHUB_WEBHOOK_SECRET={GITHUB_WEBHOOK_SECRET}\n"
    )
    yield from _start(env_file)


def _page(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
        return str(response.read().decode())


def test_send_event_then_see_it(runner: run.Running, tmp_path: Path) -> None:
    secret = run.load_or_create_secrets(tmp_path / ".env.local")["WEBHOOK_SECRET"]
    url = runner.receiver_url + "webhooks/token-keeper"

    assert send(url, secret, synthetic_event(2, "active", None, "Newer", "octo-demo"), "m1") == 204
    assert send(url, secret, synthetic_event(2, "active", None, "Dupe", "octo-demo"), "m1") == 204
    assert send(url, secret, synthetic_event(1, "active", None, "Older", "octo-demo"), "m2") == 204
    assert (
        send(
            url,
            "whsec_" + "d3Jvbmctc2VjcmV0LW5vdC1yZWFs",
            synthetic_event(9, "active", None, "Forged", "octo-demo"),
            "m3",
        )
        == 401
    )

    page = _page(runner.receiver_url)
    assert "Newer" in page
    assert not any(word in page for word in ("Dupe", "Older", "Forged"))
    assert (tmp_path / ".env.local").stat().st_mode & 0o777 == 0o600


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _location(request: urllib.request.Request | str) -> str:
    """Make one request without following redirects; return the Location header."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(request, timeout=5)
    except urllib.error.HTTPError as exc:
        assert exc.code in (302, 303), exc.code
        return str(exc.headers["location"])
    raise AssertionError("expected a redirect")


def test_runner_without_github_app_starts_only_the_receiver(runner: run.Running) -> None:
    assert runner.token_keeper_url is None


def _wait_for(condition: Callable[[], bool], seconds: float = 10) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


def test_connect_flow_end_to_end(runner_with_github: run.Running, tmp_path: Path) -> None:
    """Passcode → signed link → /connect → (GitHub, faked) → /callback → delivery → page,
    then the immediate sync's events, then a forced refresh through the local control."""
    passcode = run.load_or_create_secrets(tmp_path / ".env.local")["DEMO_PASSCODE"]
    with respx.mock(assert_all_called=False) as router:
        # Only GitHub is faked; the deliverer's POST to the local receiver goes through.
        router.route(host="localhost").pass_through()
        router.route(host="127.0.0.1").pass_through()
        token_route, _ = mock_github(router)
        mock_events(router)

        form = urllib.request.Request(  # noqa: S310 (local http URL)
            runner_with_github.receiver_url + "connect",
            data=f"passcode={passcode}".encode(),
            method="POST",
        )
        connect_url = _location(form)
        assert connect_url.startswith(f"{runner_with_github.token_keeper_url}/connect?")

        github_url = _location(connect_url)
        assert github_url.startswith("https://github.com/login/oauth/authorize?")
        state = parse_qs(urlsplit(github_url).query)["state"][0]

        back = _location(f"{runner_with_github.token_keeper_url}/callback?code=c&state={state}")
        assert back == f"{runner_with_github.receiver_url}?result=connected"

        # The drain threads deliver account.connected, then run the immediate sync and
        # deliver its account.updated, which carries the public events.
        assert _wait_for(
            lambda: "PushEvent on octo-demo/repo-1" in _page(runner_with_github.receiver_url)
        )
        page = _page(runner_with_github.receiver_url)
        assert "@octo-demo" in page and "Octo Demo" in page
        assert token_route.call_count == 1  # the code exchange only

        # /expire forces the next sync to refresh (how the live rotation check works).
        token_route.mock(
            return_value=httpx.Response(
                200, json=token_body(ROTATED_ACCESS_TOKEN, ROTATED_REFRESH_TOKEN)
            )
        )
        assert runner_with_github.control_url
        expire = urllib.request.Request(  # noqa: S310 (local http URL)
            runner_with_github.control_url + "/expire", data=b"", method="POST"
        )
        with urllib.request.urlopen(expire, timeout=5) as response:  # noqa: S310
            assert json.loads(response.read()) == {"expired": 1, "enqueued": 1}
        assert _wait_for(lambda: token_route.call_count == 2)
        sent = parse_qs(token_route.calls.last.request.content.decode())
        assert sent["grant_type"] == ["refresh_token"]

        # GitHub's revocation delivery flips the card (slice 5).
        delivery = github_delivery()
        assert runner_with_github.token_keeper_url
        revoke = urllib.request.Request(  # noqa: S310 (local http URL)
            runner_with_github.token_keeper_url + "/github/webhook",
            data=delivery["body"].encode(),
            headers=delivery["headers"],
            method="POST",
        )
        with urllib.request.urlopen(revoke, timeout=5) as response:  # noqa: S310
            assert response.status == 204
        assert _wait_for(
            lambda: "Needs re-auth (revoked)" in _page(runner_with_github.receiver_url)
        )


def test_access_log_never_prints_query_strings(
    runner: run.Running, capsys: pytest.CaptureFixture[str]
) -> None:
    _page(runner.receiver_url + "?result=connected&code=secret-code-marker")
    out = capsys.readouterr().out
    assert "GET / ->" in out
    assert "secret-code-marker" not in out


def test_demo_gif_controls(
    runner_with_github: run.Running, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /account`` and ``POST /revoke``, through the script's own LocalTarget: the
    rehearsal checks the same facts the live run reads from DynamoDB."""
    from tests.test_demo_gif import gif
    from token_keeper import sync

    assert runner_with_github.control_url
    target = gif.LocalTarget(runner_with_github.control_url)
    # The runner's default is six hours: the rehearsal refuses to start.
    with pytest.raises(gif.CheckFailed, match="SYNC_INTERVAL_MINUTES=15"):
        target.preflight()
    monkeypatch.setenv("SYNC_INTERVAL_MINUTES", "15")
    sync.reset_interval_cache()
    target.preflight()
    assert target.facts() is None

    passcode = run.load_or_create_secrets(tmp_path / ".env.local")["DEMO_PASSCODE"]
    with respx.mock(assert_all_called=False) as router:
        router.route(host="localhost").pass_through()
        router.route(host="127.0.0.1").pass_through()
        mock_github(router)
        mock_events(router)
        form = urllib.request.Request(  # noqa: S310 (local http URL)
            runner_with_github.receiver_url + "connect",
            data=f"passcode={passcode}".encode(),
            method="POST",
        )
        github_url = _location(_location(form))
        state = parse_qs(urlsplit(github_url).query)["state"][0]
        _location(f"{runner_with_github.token_keeper_url}/callback?code=c&state={state}")

        # The immediate sync applies the 15-minute knob.
        assert _wait_for(lambda: (f := target.facts()) is not None and f.fetched_at is not None)
        connected = target.facts()
        assert connected and connected.has_tokens
        gif.check_connected(None, connected, int(time.time()))

        target.revoke_stand_in()
        revoked = target.facts()
        assert revoked
        gif.check_revoked(connected, revoked)
        assert _wait_for(
            lambda: "Needs re-auth (revoked)" in _page(runner_with_github.receiver_url)
        )
    sync.reset_interval_cache()
