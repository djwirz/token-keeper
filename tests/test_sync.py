"""The demo cadence knob (ADR 0020): bounds, fallback, caching and the SSM path."""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
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


@pytest.mark.parametrize("raw", ["14", "0", "-15", "361", "abc", "15.5", "1e2"])
def test_a_bad_knob_falls_back_to_six_hours_and_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], raw: str
) -> None:
    monkeypatch.setenv("SYNC_INTERVAL_MINUTES", raw)
    assert sync.sync_interval_seconds() == SIX_HOURS
    assert "sync.interval_invalid" in capsys.readouterr().out


def test_the_value_is_cached_for_a_minute_then_reread(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setenv("SYNC_INTERVAL_MINUTES", "15")
    assert sync.sync_interval_seconds(lambda: clock[0]) == 900
    monkeypatch.setenv("SYNC_INTERVAL_MINUTES", "360")
    clock[0] += sync.INTERVAL_CACHE_SECONDS - 1
    assert sync.sync_interval_seconds(lambda: clock[0]) == 900
    clock[0] += 1
    assert sync.sync_interval_seconds(lambda: clock[0]) == SIX_HOURS


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
