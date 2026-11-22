"""The demo cadence knob (ADR 0020): bounds, fallback, caching and the SSM path."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.exceptions import ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError
from moto import mock_aws

from token_keeper import sync

PARAM = "/token-keeper/test/sync-interval-minutes"
SIX_HOURS = 6 * 3600


@pytest.fixture(autouse=True)
def clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("SYNC_INTERVAL_MINUTES", raising=False)
    monkeypatch.delenv("SYNC_INTERVAL_MINUTES_PARAM", raising=False)
    sync.reset_interval_cache()
    yield
    sync.reset_interval_cache()


def test_unset_means_six_hours(capsys: pytest.CaptureFixture[str]) -> None:
    assert sync.sync_interval_seconds() == SIX_HOURS
    assert "sync.interval" not in capsys.readouterr().out


@pytest.mark.parametrize(("raw", "seconds"), [("15", 900), (" 60 ", 3600), ("360", SIX_HOURS)])
def test_values_in_bounds_are_used(monkeypatch: pytest.MonkeyPatch, raw: str, seconds: int) -> None:
    monkeypatch.setenv("SYNC_INTERVAL_MINUTES", raw)
    assert sync.sync_interval_seconds() == seconds


@pytest.mark.parametrize(
    "raw", ["14", "0", "-15", "361", "abc", "15.5", "1e2", "+15", "1_5", "\u0661\u0665"]
)
def test_a_bad_knob_falls_back_to_six_hours_and_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], raw: str
) -> None:
    monkeypatch.setenv("SYNC_INTERVAL_MINUTES", raw)
    assert sync.sync_interval_seconds() == SIX_HOURS
    assert "sync.interval_invalid" in capsys.readouterr().out


def test_the_value_is_cached_for_a_minute_then_reread(monkeypatch: pytest.MonkeyPatch) -> None:
    # The same call the worker makes, with no clock passed in: only the module's own
    # clock moves (PR 11 review: an injected clock left the real path untested).
    clock = [1000.0]
    monkeypatch.setattr(sync, "_now", lambda: clock[0])
    monkeypatch.setenv("SYNC_INTERVAL_MINUTES", "15")
    assert sync.sync_interval_seconds() == 900
    monkeypatch.setenv("SYNC_INTERVAL_MINUTES", "360")
    clock[0] += sync.INTERVAL_CACHE_SECONDS - 1
    assert sync.sync_interval_seconds() == 900
    clock[0] += 1
    assert sync.sync_interval_seconds() == SIX_HOURS


def test_the_cache_follows_wall_clock_time() -> None:
    # Not time.monotonic: whether it advances while Lambda freezes a sandbox between
    # invocations isn't documented.
    assert sync._now is time.time


def test_a_fallback_is_cached_too(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sync, "_now", lambda: 1000.0)
    monkeypatch.setenv("SYNC_INTERVAL_MINUTES", "abc")
    for _ in range(3):
        assert sync.sync_interval_seconds() == SIX_HOURS
    assert capsys.readouterr().out.count("sync.interval_invalid") == 1


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    with mock_aws():
        yield


def test_in_aws_the_parameter_is_read(aws: None, monkeypatch: pytest.MonkeyPatch) -> None:
    boto3.client("ssm").put_parameter(Name=PARAM, Value="15", Type="String")
    monkeypatch.setenv("SYNC_INTERVAL_MINUTES_PARAM", PARAM)
    assert sync.sync_interval_seconds() == 900


@pytest.mark.parametrize(
    "error",
    [
        EndpointConnectionError(endpoint_url="https://ssm.invalid"),
        ConnectTimeoutError(endpoint_url="https://ssm.invalid"),
        ReadTimeoutError(endpoint_url="https://ssm.invalid"),
    ],
)
def test_a_network_failure_never_stops_syncs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: Exception
) -> None:
    # What a hang or a network blip in AWS raises: BotoCoreErrors, not the ClientError
    # moto gives for a missing parameter (PR 11 review).
    def fail(name: str, config: object = None) -> str | None:
        assert config is sync.INTERVAL_READ_CONFIG  # the fast client, not the defaults
        raise error

    monkeypatch.setattr(sync, "optional_env_or_ssm", fail)
    assert sync.sync_interval_seconds() == SIX_HOURS
    assert "sync.interval_unreadable" in capsys.readouterr().out


def test_the_ssm_read_fails_fast() -> None:
    # The worker has 40 seconds; botocore's defaults would wait a minute per attempt.
    # Checked on a real client, since botocore rewrites the retries dict in place.
    client = boto3.client("ssm", region_name="us-east-1", config=sync.INTERVAL_READ_CONFIG)
    config: Any = client.meta.config  # botocore's stubs don't list these attributes
    assert config.connect_timeout == config.read_timeout == 2
    assert config.retries is not None
    assert config.retries["total_max_attempts"] == 3  # the first try and two retries


def test_an_unreadable_parameter_never_stops_syncs(
    aws: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SYNC_INTERVAL_MINUTES_PARAM", PARAM)  # never created
    assert sync.sync_interval_seconds() == SIX_HOURS
    assert "sync.interval_unreadable" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("interval", "jitter"), [(SIX_HOURS, 30 * 60), (3600, 15 * 60), (900, 225)]
)
def test_jitter_is_half_an_hour_or_a_quarter_of_the_interval(interval: int, jitter: int) -> None:
    assert sync.jitter_seconds(interval) == jitter
    dues = {sync.next_sync_at(0, interval) for _ in range(500)}
    assert min(dues) >= interval - jitter
    assert max(dues) <= interval + jitter
    # Spread across the window, not stuck at one end.
    assert min(dues) < interval - jitter // 2 < interval + jitter // 2 < max(dues)


def test_next_sync_at_reads_the_knob(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNC_INTERVAL_MINUTES", "15")
    assert 900 - 225 <= sync.next_sync_at(0) <= 900 + 225
