"""``scripts/demo_gif.py``: the README GIF's guards and checks, without a browser.

What matters most: the script refuses to start on the wrong knob, each clip's check
fails on the wrong real state, and no GIF is written unless every check passed.
The browser half is proven by the rehearsal against the local runner (docs/deploy.md).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from local.facts import Facts


def _script() -> ModuleType:
    path = Path(__file__).resolve().parent.parent / "scripts" / "demo_gif.py"
    spec = importlib.util.spec_from_file_location("demo_gif", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["demo_gif"] = module  # dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


gif = _script()
NOW = 1_800_000_000


def facts(**changes: Any) -> Facts:
    base: dict[str, Any] = {
        "account_id": "acc_demo",
        "state": "active",
        "reason": None,
        "version": 5,
        "next_due_at": NOW + 15 * 60,
        "has_tokens": True,
        "fetched_at": "2026-11-23T10:00:00Z",
    }
    return Facts(**{**base, **changes})


# --- The knob guard -----------------------------------------------------------------


def test_knob_at_15_for_over_a_minute_passes() -> None:
    now = datetime(2026, 11, 23, 12, tzinfo=UTC)
    gif.check_knob("15", now - timedelta(seconds=61), now)
    gif.check_knob(" 15\n", now - timedelta(hours=1), now)


@pytest.mark.parametrize("value", ["360", "16", "", "15 minutes", "015"])
def test_knob_not_15_refuses(value: str) -> None:
    now = datetime(2026, 11, 23, 12, tzinfo=UTC)
    with pytest.raises(gif.CheckFailed, match="knob reads"):
        gif.check_knob(value, now - timedelta(hours=1), now)


def test_knob_changed_inside_the_minute_refuses() -> None:
    """A warm worker can still hold six hours: connecting now would stall the run."""
    now = datetime(2026, 11, 23, 12, tzinfo=UTC)
    with pytest.raises(gif.CheckFailed, match="a minute"):
        gif.check_knob("15", now - timedelta(seconds=30), now)


# --- Each clip's check ----------------------------------------------------------------


def test_connected_passes_with_the_knob_applied() -> None:
    gif.check_connected(facts(version=4), facts(), NOW)
    gif.check_connected(None, facts(), NOW)  # a first-ever connect


@pytest.mark.parametrize(
    ("after", "match"),
    [
        (facts(next_due_at=NOW + 6 * 3600), "didn't take"),
        (facts(next_due_at=NOW + 60), "minutes out"),
        (facts(next_due_at=None), "no next due"),
        (facts(state="needs_reauth", reason="revoked"), "not active"),
        (facts(has_tokens=False), "no tokens"),
        (facts(version=4), "hasn't moved"),
    ],
)
def test_connected_fails_on_the_wrong_state(after: Facts, match: str) -> None:
    with pytest.raises(gif.CheckFailed, match=match):
        gif.check_connected(facts(version=4), after, NOW)


def test_synced_needs_a_new_snapshot_and_version() -> None:
    before = facts()
    gif.check_synced(before, facts(version=6, fetched_at="2026-11-23T10:20:00Z"))
    with pytest.raises(gif.CheckFailed, match="no sync"):
        gif.check_synced(before, facts(version=6))
    with pytest.raises(gif.CheckFailed, match="hasn't moved"):
        gif.check_synced(before, facts(fetched_at="2026-11-23T10:20:00Z"))
    assert not gif.synced(before, None)
    assert gif.synced(before, facts(version=6, fetched_at="2026-11-23T10:20:00Z"))


def test_revoked_means_tokens_gone() -> None:
    before = facts()
    gif.check_revoked(
        before, facts(state="needs_reauth", reason="revoked", has_tokens=False, version=6)
    )
    with pytest.raises(gif.CheckFailed, match="still holds tokens"):
        gif.check_revoked(before, facts(state="needs_reauth", reason="revoked", version=6))
    with pytest.raises(gif.CheckFailed, match="not revoked"):
        gif.check_revoked(
            before,
            facts(state="needs_reauth", reason="refresh_failed", has_tokens=False, version=6),
        )
    with pytest.raises(gif.CheckFailed, match="hasn't moved"):
        gif.check_revoked(before, facts(state="needs_reauth", reason="revoked", has_tokens=False))


def test_reconnected_needs_tokens_back() -> None:
    revoked = facts(state="needs_reauth", reason="revoked", has_tokens=False, version=6)
    gif.check_reconnected(revoked, facts(version=7))
    with pytest.raises(gif.CheckFailed, match="not active"):
        gif.check_reconnected(revoked, facts(state="needs_reauth", reason="revoked", version=7))
    with pytest.raises(gif.CheckFailed, match="no tokens"):
        gif.check_reconnected(revoked, facts(has_tokens=False, version=7))


# --- Segments, the filter and the encode -------------------------------------------------


def test_filter_trims_and_joins_each_kept_segment() -> None:
    segments = [gif.Segment(0, 1.0, 4.5), gif.Segment(1, 0.5, 2.0), gif.Segment(1, 3.0, 6.25)]
    gif.validate_segments(segments, 2)
    text = gif.ffmpeg_filter(segments)
    assert "[0:v]trim=start=1.00:end=4.50,setpts=PTS-STARTPTS[c0]" in text
    assert "[1:v]trim=start=3.00:end=6.25,setpts=PTS-STARTPTS[c2]" in text
    assert "[c0][c1][c2]concat=n=3:v=1:a=0,fps=10,scale=720:-1" in text
    assert text.endswith("paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle")


@pytest.mark.parametrize(
    "segments",
    [
        [],
        [gif.Segment(0, 2.0, 2.0)],
        [gif.Segment(0, 3.0, 1.0)],
        [gif.Segment(0, 1.0, 4.0), gif.Segment(0, 3.0, 5.0)],
        [gif.Segment(2, 0.0, 1.0)],
    ],
)
def test_bad_segments_refuse(segments: list[Any]) -> None:
    with pytest.raises(gif.CheckFailed):
        gif.validate_segments(segments, 2)


def _manifest(tmp_path: Path, passed: Any) -> Path:
    path = tmp_path / "manifest.json"
    data = gif.Manifest(True, [str(tmp_path / "a.webm")], [gif.Segment(0, 0.5, 3.0)]).to_json()
    data["passed"] = passed
    path.write_text(json.dumps(data))
    return path


@pytest.mark.parametrize("passed", [False, None, "true", 1])
def test_no_gif_unless_the_manifest_passed(tmp_path: Path, passed: Any) -> None:
    out = tmp_path / "docs" / "demo.gif"
    calls: list[list[str]] = []
    with pytest.raises(gif.CheckFailed, match="passed"):
        gif.encode(_manifest(tmp_path, passed), out, run=lambda c, **_: calls.append(c))
    assert calls == []
    assert not out.exists()


def test_no_manifest_no_gif(tmp_path: Path) -> None:
    out = tmp_path / "demo.gif"
    with pytest.raises(gif.CheckFailed, match="record first"):
        gif.encode(tmp_path / "missing.json", out, run=lambda c, **_: None)
    assert not out.exists()


def test_a_passing_manifest_encodes_in_the_pinned_image(tmp_path: Path) -> None:
    out = tmp_path / "docs" / "demo.gif"
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> None:
        calls.append(command)
        if command[-1] == "demo.gif":
            (tmp_path / "demo.gif").write_bytes(b"GIF89a")

    gif.encode(_manifest(tmp_path, True), out, run=fake_run)
    assert out.read_bytes() == b"GIF89a"
    gif_call, sheet_call = calls
    assert gif.FFMPEG in gif_call and "@sha256:" in gif.FFMPEG
    assert gif_call[3:5] == ["--network", "none"]
    assert gif_call[gif_call.index("-i") + 1] == "a.webm"  # relative to the mount
    assert sheet_call[-1] == "contact-sheet.png"


def test_ffmpeg_that_writes_nothing_leaves_no_gif(tmp_path: Path) -> None:
    out = tmp_path / "docs" / "demo.gif"
    with pytest.raises(gif.CheckFailed, match="no GIF"):
        gif.encode(_manifest(tmp_path, True), out, run=lambda c, **_: None)
    assert not out.exists()


# --- Small helpers ----------------------------------------------------------------------


def test_errors_mask_the_aws_account() -> None:
    mask = gif.Masker("123456789012")
    text = gif.mask_identity("arn:aws:dynamodb:us-east-1:123456789012:table/x", mask)
    assert "123456789012" not in text and "<account>" in text


def test_human_only_pages() -> None:
    for url in [
        "https://github.com/login",
        "https://github.com/login?return_to=x",
        "https://github.com/sessions/two-factor/app",
        "https://github.com/session",
    ]:
        assert gif.HUMAN_ONLY.match(url), url
    for url in [
        "https://github.com/login/oauth/authorize?client_id=Iv",
        "https://github.com/settings/connections/applications/Iv",
    ]:
        assert not gif.HUMAN_ONLY.match(url), url


def test_client_id_comes_from_env_local(tmp_path: Path) -> None:
    env = tmp_path / ".env.local"
    env.write_text("OTHER=x\nGITHUB_CLIENT_ID='Iv23-test'\n")
    assert gif.client_id_from_env(env) == "Iv23-test"
    env.write_text("OTHER=x\n")
    with pytest.raises(gif.CheckFailed, match="GITHUB_CLIENT_ID"):
        gif.client_id_from_env(env)
