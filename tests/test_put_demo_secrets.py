"""``put_demo_secrets.py --github-webhook-secret`` (slice 5): SSM and clipboard, no output."""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import boto3
import pytest
from moto import mock_aws

from infra.stacks.token_keeper import SECRET_PARAMS
from token_keeper import settings

NAME = SECRET_PARAMS["GITHUB_WEBHOOK_SECRET_PARAM"]


def _script() -> ModuleType:
    path = Path(__file__).resolve().parent.parent / "scripts" / "put_demo_secrets.py"
    spec = importlib.util.spec_from_file_location("put_demo_secrets", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ssm(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    for key, value in {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    with mock_aws():
        yield boto3.client("ssm", region_name="us-east-1")


@pytest.fixture
def script() -> ModuleType:
    return _script()


@pytest.fixture
def clipboard(script: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What the script sends to ``pbcopy``, captured instead."""
    copied: list[str] = []
    monkeypatch.setattr(script, "to_clipboard", copied.append)
    return copied


def test_writes_a_securestring_copies_it_and_prints_nothing_secret(
    ssm: Any, script: ModuleType, clipboard: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    script.put_github_webhook_secret(ssm, overwrite=False)

    param = ssm.get_parameter(Name=NAME, WithDecryption=True)["Parameter"]
    assert param["Type"] == "SecureString"
    value = param["Value"]
    assert clipboard == [value]
    # Long enough for the Lambda's own startup check, and never in the output.
    assert len(value) >= settings.MIN_GITHUB_WEBHOOK_SECRET_CHARS
    out = capsys.readouterr()
    assert value not in out.out + out.err
    assert NAME in out.out


def test_refuses_to_replace_one_without_overwrite(
    ssm: Any, script: ModuleType, clipboard: list[str]
) -> None:
    script.put_github_webhook_secret(ssm, overwrite=False)
    first = ssm.get_parameter(Name=NAME, WithDecryption=True)["Parameter"]["Value"]
    with pytest.raises(SystemExit):
        script.put_github_webhook_secret(ssm, overwrite=False)
    assert ssm.get_parameter(Name=NAME, WithDecryption=True)["Parameter"]["Value"] == first

    script.put_github_webhook_secret(ssm, overwrite=True)
    second = ssm.get_parameter(Name=NAME, WithDecryption=True)["Parameter"]["Value"]
    assert second != first and clipboard == [first, second]


def test_a_failed_clipboard_writes_nothing(
    ssm: Any, script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Otherwise SSM would hold a secret nobody has, and a rerun would refuse (PR 6 review).
    def broken(value: str) -> None:
        raise OSError("pbcopy not found")

    monkeypatch.setattr(script, "to_clipboard", broken)
    with pytest.raises(SystemExit):
        script.put_github_webhook_secret(ssm, overwrite=False)
    assert not script.exists(ssm, NAME)
