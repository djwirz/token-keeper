"""Smoke test: the real local runner, over HTTP, against a moto server."""

from __future__ import annotations

import os
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from local import run
from local.send_event import send, synthetic_event
from receiver import config, handler


@pytest.fixture
def runner(tmp_path: Path) -> Iterator[run.Running]:
    saved = dict(os.environ)
    config.load.cache_clear()
    handler._store.cache_clear()
    running = run.start(receiver_port=run.free_port(), env_file=tmp_path / ".env.local")
    try:
        yield running
    finally:
        running.stop()
        os.environ.clear()
        os.environ.update(saved)
        config.load.cache_clear()
        handler._store.cache_clear()


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
