"""No token-shaped string ever reaches a log line (spec §8, §11.5)."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Iterator

import httpx
import pytest
import respx

from tests.conftest import RECEIVER_WEBHOOK_URL, TokenKeeperEnv
from tests.github_fakes import ACCESS_TOKEN, REFRESH_TOKEN, mock_github
from tests.tk_flow import connect_and_callback, drain
from token_keeper import log


def test_scrub_replaces_every_github_token_prefix() -> None:
    for prefix in ("ghu_", "ghr_", "ghp_", "gho_", "ghs_"):
        assert log.scrub(f"before {prefix}abc123_XYZ after") == f"before {log.REDACTED} after"


def test_scrub_replaces_base64_tokens() -> None:
    # As botocore logs a KMS body at DEBUG: the plaintext token, base64-encoded.
    for token in (ACCESS_TOKEN, REFRESH_TOKEN, "ghp_" + "x", "gho_" + "x", "ghs_" + "x"):
        encoded = base64.b64encode(token.encode()).decode()
        body = f'{{"KeyId": "k", "Plaintext": "{encoded}"}}'
        assert log.scrub(body) == f'{{"KeyId": "k", "Plaintext": "{log.REDACTED}"}}'


def test_formatter_scrubs_message_extra_keys_and_tracebacks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logger = log.start(handler="test", leaked_key=ACCESS_TOKEN)
    logger.info(f"message with {ACCESS_TOKEN}", extra={"field": REFRESH_TOKEN})
    try:
        raise ValueError(f"oops {REFRESH_TOKEN}")
    except ValueError:
        logger.exception("failed")
    logging.getLogger("httpx").warning("third-party line %s", ACCESS_TOKEN)
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.splitlines()]
    assert len(lines) == 3
    assert "ghu_" not in out and "ghr_" not in out
    assert out.count(log.REDACTED) >= 5


@pytest.fixture
def everything_logged() -> Iterator[None]:
    """Turn every logger up to DEBUG for the flow test, so nothing is filtered out.

    That includes botocore, httpx and httpcore, which log request and response bodies
    at DEBUG. ``log.get_logger()`` pins them to WARNING, so setting the root logger's
    level alone would leave them silent (the first version of this test did that).
    """
    log.get_logger()  # copy the scrubbing config onto third-party loggers first
    loggers = [logging.getLogger()] + [
        lg for lg in logging.root.manager.loggerDict.values() if isinstance(lg, logging.Logger)
    ]
    previous = {lg: lg.level for lg in loggers}
    for lg in loggers:
        lg.setLevel(logging.DEBUG)
    yield
    for lg, level in previous.items():
        lg.setLevel(level)


@pytest.mark.usefixtures("everything_logged")
def test_whole_flow_logs_no_tokens(
    tk_env: TokenKeeperEnv,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as router:
        mock_github(router)
        router.post(RECEIVER_WEBHOOK_URL).mock(return_value=httpx.Response(204))
        connect_and_callback()
        connect_and_callback()  # reconnect path too
        drain(tk_env.queue_url)

    out = capsys.readouterr()
    emitted = out.out + out.err
    assert "callback.connected" in emitted and "delivery.succeeded" in emitted
    # Third-party DEBUG output really is in the capture, KMS bodies included.
    assert '"name":"botocore' in emitted and "Plaintext" in emitted
    assert "ghu_" not in emitted and "ghr_" not in emitted
    for token in (ACCESS_TOKEN, REFRESH_TOKEN):
        assert base64.b64encode(token.encode()).decode()[:12] not in emitted
    # caplog sees records before formatting, so it also proves no code logs a token
    # in the first place (the scrubber is only the backstop).
    raw = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert ACCESS_TOKEN not in raw and REFRESH_TOKEN not in raw
