"""Smoke test: the real local runner, over HTTP, against a moto server."""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import respx

from local import run
from local.send_event import send, synthetic_event
from receiver import config, handler
from tests.github_fakes import mock_github
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
        receiver_port=run.free_port(), env_file=env_file, token_keeper_port=run.free_port()
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
    env_file.write_text("GITHUB_CLIENT_ID=Iv23-test\nGITHUB_CLIENT_SECRET=test-not-real\n")
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


def test_connect_flow_end_to_end(runner_with_github: run.Running, tmp_path: Path) -> None:
    """Passcode → signed link → /connect → (GitHub, faked) → /callback → delivery → page."""
    passcode = run.load_or_create_secrets(tmp_path / ".env.local")["DEMO_PASSCODE"]
    with respx.mock(assert_all_called=False) as router:
        # Only GitHub is faked; the deliverer's POST to the local receiver goes through.
        router.route(host="localhost").pass_through()
        mock_github(router)

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

        # The drain thread delivers account.connected to the receiver.
        deadline = time.monotonic() + 10
        page = ""
        while time.monotonic() < deadline:
            page = _page(runner_with_github.receiver_url)
            if "@octo-demo" in page:
                break
            time.sleep(0.2)
    assert "@octo-demo" in page
    assert "Octo Demo" in page


def test_access_log_never_prints_query_strings(
    runner: run.Running, capsys: pytest.CaptureFixture[str]
) -> None:
    _page(runner.receiver_url + "?result=connected&code=secret-code-marker")
    out = capsys.readouterr().out
    assert "GET / ->" in out
    assert "secret-code-marker" not in out
