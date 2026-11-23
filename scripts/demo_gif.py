"""The README GIF (spec §11, criterion 1), recorded from the real system and checked.

    uv run python scripts/demo_gif.py sign-in                        # once, by hand
    uv run python scripts/demo_gif.py record --profile token-keeper  # live, on demo
    uv run python scripts/demo_gif.py record --local [--nudge]       # rehearsal
    uv run python scripts/demo_gif.py encode                         # re-encode only

Playwright drives a headed Chromium with a persistent profile outside the repo, where
Dan signed in to GitHub once by hand (``sign-in``). ``record`` plays four clips:
connect, a scheduled sync, revoke in GitHub's settings, reconnect. After each one it
checks the page and the account's real state (DynamoDB, read-only; or the local
runner's ``GET /account``). Only when every check has passed does it write a manifest
of the kept video segments, and only a passing manifest is turned into
``docs/media/demo.gif``, by ffmpeg in Docker, an image pinned by digest.

What's by hand, and cut from the GIF: the demo passcode (the script never types a
credential), GitHub sign-in, and any sudo or bot check GitHub asks for. The wait for
the scheduled sync isn't recorded at all.

The cadence knob (ADR 0020) is set outside this script, on Dan's "go". ``record``
refuses to start unless it reads 15 and was changed over a minute ago, so no warm
worker still holds the six-hour value. ``--local`` walks GitHub's revoke page without
clicking Revoke (so ``demo`` is untouched) and calls the runner's ``POST /revoke``.

Output: account ids, states, versions and times. No token is read; the AWS account ID
is masked.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from local.facts import Facts, read_facts  # noqa: E402

REGION = "us-east-1"
KNOB = "/token-keeper/demo/token-keeper/sync-interval-minutes"
RECEIVER_URL_PARAM = "/token-keeper/demo/shared/receiver-url"
DEMO_MINUTES = 15
# A warm worker caches the knob for 60 s (ADR 0020); a connect inside that minute
# schedules the next sync six hours out.
KNOB_SETTLE_SECONDS = 60
# 15 minutes with ±3¾ of jitter, and a little slack for the time the checks take.
DUE_WINDOW = (10 * 60, 20 * 60)
SYNC_WAIT_SECONDS = 40 * 60
REVOKE_FLIP_SECONDS = 60
FFMPEG = (
    "jrottenberg/ffmpeg:9.0.1-alpine320"
    "@sha256:2c3d71587b80173373ff52f2573ec5be9f527f2ba4fe601de6641299b1d68211"
)
STATE_HOME = Path.home() / "Library" / "Application Support" / "token-keeper"
PROFILE = STATE_HOME / "demo-gif-profile"
VIDEOS = STATE_HOME / "demo-gif-videos"
MANIFEST = VIDEOS / "manifest.json"
OUT = ROOT / "docs" / "media" / "demo.gif"
VIEWPORT = {"width": 720, "height": 800}
GIF_WIDTH = 640
FPS = 10
LOCAL_RECEIVER = "http://localhost:8000/"
LOCAL_CONTROL = "http://127.0.0.1:8002"
GITHUB = "https://github.com"
# GitHub pages that need a person: sign-in, 2FA, sudo ("confirm access"), device checks.
HUMAN_ONLY = re.compile(r"^https://github\.com/(login|session|sessions/|sudo)(?!/oauth/authorize)")


class CheckFailed(Exception):
    """A check against the page or the account's real state failed: no GIF."""


# --- Pure checks (tested without a browser) -------------------------------------------


def check_knob(value: str, modified: datetime, now: datetime) -> None:
    if value.strip() != str(DEMO_MINUTES):
        raise CheckFailed(
            f"the knob reads {value.strip()!r}, not {DEMO_MINUTES}: set it first, on Dan's go"
        )
    age = (now - modified).total_seconds()
    if age < KNOB_SETTLE_SECONDS:
        raise CheckFailed(
            f"the knob changed {int(age)} s ago: wait until a minute has passed, "
            "so no warm worker still holds the old value"
        )


def check_connected(before: Facts | None, after: Facts, now: int) -> None:
    """Clip 1, and clip 4 with ``due=False``: active, written, holding tokens."""
    _check_reconnected(before, after)
    if after.next_due_at is None:
        raise CheckFailed("the account has no next due time")
    ahead = after.next_due_at - now
    low, high = DUE_WINDOW
    if not low <= ahead <= high:
        raise CheckFailed(
            f"the next sync is {ahead // 60} minutes out, not about {DEMO_MINUTES}: "
            "the knob didn't take (connected inside the first minute?)"
        )


def check_reconnected(before: Facts | None, after: Facts) -> None:
    _check_reconnected(before, after)


def _check_reconnected(before: Facts | None, after: Facts) -> None:
    if after.state != "active":
        raise CheckFailed(f"the account is {after.state} ({after.reason}), not active")
    if not after.has_tokens:
        raise CheckFailed("the account is active but holds no tokens")
    _check_written(before, after)


def check_synced(before: Facts, after: Facts) -> None:
    """Clip 2: a scheduled sync wrote a new snapshot, with no click."""
    if after.state != "active":
        raise CheckFailed(f"the account is {after.state} ({after.reason}), not active")
    if after.fetched_at is None or after.fetched_at == before.fetched_at:
        raise CheckFailed("the snapshot hasn't moved: no sync yet")
    _check_written(before, after)


def check_revoked(before: Facts, after: Facts) -> None:
    """Clip 3: the revocation took over the account and deleted its tokens."""
    if (after.state, after.reason) != ("needs_reauth", "revoked"):
        raise CheckFailed(f"the account is {after.state} ({after.reason}), not revoked")
    if after.has_tokens:
        raise CheckFailed("the account was revoked but still holds tokens")
    _check_written(before, after)


def _check_written(before: Facts | None, after: Facts) -> None:
    if before is not None and after.version <= before.version:
        raise CheckFailed(f"version {after.version} hasn't moved on from {before.version}")


def synced(before: Facts, after: Facts | None) -> bool:
    if after is None:
        return False
    try:
        check_synced(before, after)
    except CheckFailed:
        return False
    return True


# --- Segments and the encode (pure, tested) -------------------------------------------


@dataclass(frozen=True)
class Segment:
    video: int  # index into Manifest.videos
    start: float
    end: float


@dataclass
class Manifest:
    passed: bool
    videos: list[str]
    segments: list[Segment]
    timings: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "videos": self.videos,
            "segments": [s.__dict__ for s in self.segments],
            "timings": self.timings,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Manifest:
        return cls(
            passed=data.get("passed") is True,
            videos=list(data["videos"]),
            segments=[Segment(**s) for s in data["segments"]],
            timings=dict(data.get("timings") or {}),
        )


def validate_segments(segments: list[Segment], videos: int) -> None:
    if not segments:
        raise CheckFailed("no segments to keep")
    last: dict[int, float] = {}
    for s in segments:
        if not 0 <= s.video < videos:
            raise CheckFailed(f"segment refers to video {s.video}, of {videos}")
        if s.start < 0 or s.end <= s.start:
            raise CheckFailed(f"empty or backwards segment {s.start:.2f} to {s.end:.2f}")
        if s.start < last.get(s.video, 0.0):
            raise CheckFailed("segments overlap or run backwards in their video")
        last[s.video] = s.end


def ffmpeg_filter(segments: list[Segment]) -> str:
    """Trim each kept segment, join them, then holdfast's one-palette GIF filter."""
    parts = [
        f"[{s.video}:v]trim=start={s.start:.2f}:end={s.end:.2f},setpts=PTS-STARTPTS[c{i}]"
        for i, s in enumerate(segments)
    ]
    joined = "".join(f"[c{i}]" for i in range(len(segments)))
    parts.append(
        f"{joined}concat=n={len(segments)}:v=1:a=0,fps={FPS},"
        f"scale={GIF_WIDTH}:-1:flags=lanczos,split[s0][s1]"
    )
    parts.append("[s0]palettegen=max_colors=96:stats_mode=diff[p]")
    parts.append("[s1][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle")
    return ";".join(parts)


def docker_run(workdir: Path, args: list[str], uid: int, gid: int) -> list[str]:
    return [
        "docker", "run", "--rm", "--network", "none",
        "--user", f"{uid}:{gid}",
        "-v", f"{workdir}:/work", "-w", "/work",
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args,
    ]  # fmt: skip


def encode_commands(manifest: Manifest, uid: int, gid: int) -> list[list[str]]:
    """The GIF, then a contact sheet of one frame a second for the eye check. Both are
    written inside the videos folder; the GIF only moves into the repo after."""
    if not manifest.passed:
        raise CheckFailed("the manifest isn't marked passed: no GIF")
    validate_segments(manifest.segments, len(manifest.videos))
    inputs = [arg for v in manifest.videos for arg in ("-i", Path(v).name)]
    gif = [*inputs, "-filter_complex", ffmpeg_filter(manifest.segments), "-loop", "0", "demo.gif"]
    sheet = [
        "-i", "demo.gif",
        "-vf", "fps=1,scale=320:-1,tile=6x6:padding=4:color=white",
        "-frames:v", "1", "contact-sheet.png",
    ]  # fmt: skip
    return [docker_run(VIDEOS, gif, uid, gid), docker_run(VIDEOS, sheet, uid, gid)]


def encode(
    manifest_path: Path = MANIFEST, out: Path = OUT, run: Callable[..., Any] = subprocess.run
) -> Path:
    if not manifest_path.exists():
        raise CheckFailed(f"no manifest at {manifest_path}: record first")
    manifest = Manifest.from_json(json.loads(manifest_path.read_text()))
    commands = encode_commands(manifest, os.getuid(), os.getgid())
    built = manifest_path.parent / "demo.gif"
    built.unlink(missing_ok=True)
    for command in commands:
        run(command, check=True)
    if not built.exists():
        raise CheckFailed("ffmpeg wrote no GIF")
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(built, out)
    return out


# --- Reading the real state -----------------------------------------------------------


class Target(Protocol):
    receiver_url: str
    local: bool

    def preflight(self) -> None: ...

    def facts(self) -> Facts | None: ...

    def revoke_stand_in(self) -> None: ...

    def nudge(self) -> None: ...


class Masker:
    def __init__(self, account: str) -> None:
        self.account = account

    def __call__(self, text: str) -> str:
        return text.replace(self.account, "<account>") if self.account else text


def _account_ops() -> Any:
    """``scripts/account_ops.py``'s stack lookups, reused rather than copied."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "account_ops", ROOT / "scripts" / "account_ops.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AwsTarget:
    local = False

    def __init__(self, profile: str) -> None:
        import boto3

        ops = _account_ops()
        self.session = boto3.Session(profile_name=profile, region_name=REGION)
        self.mask = Masker(self.session.client("sts").get_caller_identity()["Account"])
        self.ssm = self.session.client("ssm")
        self.ddb = self.session.client("dynamodb")
        resources = ops.stack_resources(self.session, "TokenKeeper")
        self.table = ops.physical(resources, "AWS::DynamoDB::Table", "Table")
        self.receiver_url = self.ssm.get_parameter(Name=RECEIVER_URL_PARAM)["Parameter"]["Value"]

    def preflight(self) -> None:
        knob = self.ssm.get_parameter(Name=KNOB)["Parameter"]
        check_knob(knob["Value"], knob["LastModifiedDate"], datetime.now(UTC))
        if self.facts() is None:
            raise CheckFailed("no demo account yet: connect once by hand first")

    def facts(self) -> Facts | None:
        return read_facts(self.ddb, self.table)

    def revoke_stand_in(self) -> None:
        raise AssertionError("live runs revoke on GitHub")

    def nudge(self) -> None:
        raise AssertionError("live runs wait for the real planner")


class LocalTarget:
    local = True
    receiver_url = LOCAL_RECEIVER

    def __init__(self, control: str = LOCAL_CONTROL) -> None:
        self.control = control

    def _call(self, method: str, path: str) -> dict[str, Any]:
        request = urllib.request.Request(  # noqa: S310 (the local runner, http on loopback)
            self.control + path, data=b"" if method == "POST" else None, method=method
        )
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            body: dict[str, Any] = json.loads(response.read())
            return body

    def preflight(self) -> None:
        minutes = self._call("GET", "/account")["sync_interval_minutes"]
        if minutes != DEMO_MINUTES:
            raise CheckFailed(
                f"the local runner syncs every {minutes} minutes: "
                f"start it with SYNC_INTERVAL_MINUTES={DEMO_MINUTES}"
            )

    def facts(self) -> Facts | None:
        account = self._call("GET", "/account")["account"]
        return Facts.from_json(account) if account else None

    def revoke_stand_in(self) -> None:
        self._call("POST", "/revoke")

    def nudge(self) -> None:
        self._call("POST", "/expire")


def mask_identity(text: str, mask: Callable[[str], str] | None) -> str:
    return mask(text) if mask else text


# --- The browser -----------------------------------------------------------------------


CAPTION = """<!doctype html><meta charset="utf-8">
<body style="margin:0;height:100vh;display:grid;place-items:center;background:#111827;
color:#f9fafb;font:600 30px/1.4 system-ui,sans-serif;text-align:center">
<div>{title}<div style="font-weight:400;font-size:20px;color:#9ca3af;margin-top:.5rem">
{sub}</div></div>
"""


class Recorder:
    """One recorded page at a time; kept segments are timed against its video clock."""

    def __init__(self, context: Any) -> None:
        self.context = context
        self.pages: list[Any] = []
        self.segments: list[Segment] = []
        self.page: Any = None
        self._opened = 0.0
        self._start: float | None = None

    def new_page(self) -> Any:
        self.cut()
        if self.page is not None:
            self.page.close()
        self.page = self.context.new_page()
        self._opened = time.monotonic()
        self.pages.append(self.page)
        return self.page

    def close_page(self) -> None:
        self.cut()
        if self.page is not None:
            self.page.close()
            self.page = None

    def keep(self) -> None:
        if self._start is None:
            self._start = time.monotonic() - self._opened

    def cut(self) -> None:
        if self._start is not None:
            end = time.monotonic() - self._opened
            if end > self._start:
                self.segments.append(Segment(len(self.pages) - 1, self._start, end))
            self._start = None

    @contextmanager
    def clip(self) -> Iterator[Any]:
        self.keep()
        try:
            yield self.page
        finally:
            self.cut()

    def by_hand(self, what: str) -> None:
        """Cut, let Dan do something in the browser, and carry on recording after."""
        was_keeping = self._start is not None
        self.cut()
        input(f"\n>>> {what}, then press Enter here. ")
        if was_keeping:
            self.keep()

    def caption(self, title: str, sub: str = "", seconds: float = 2.0) -> None:
        self.page.set_content(CAPTION.format(title=title, sub=sub))
        with self.clip():
            self.page.wait_for_timeout(int(seconds * 1000))

    def video_paths(self) -> list[str]:
        return [str(Path(p.video.path())) for p in self.pages]


def launch(playwright: Any, record: bool) -> Any:
    PROFILE.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = {"headless": False, "viewport": VIEWPORT}
    if record:
        options["record_video_dir"] = str(VIDEOS)
        options["record_video_size"] = VIEWPORT
    return playwright.chromium.launch_persistent_context(str(PROFILE), **options)


def card_state(page: Any) -> str:
    return str(page.locator(".card .state").first.inner_text()).strip()


def last_synced(page: Any) -> str:
    text = str(page.get_by_text(re.compile(r"^Last synced ")).first.inner_text())
    return text.split("·")[0].strip()


def needs_person(page: Any) -> bool:
    """Sign-in and 2FA have their own URLs; sudo ("Confirm access") renders in place."""
    if HUMAN_ONLY.match(page.url):
        return True
    return bool(page.url.startswith(GITHUB) and page.get_by_text("Confirm access").count())


def follow_github(rec: Recorder, receiver_url: str, timeout: float = 120) -> None:
    """From the receiver's Connect, through GitHub, and back. Clicks Authorize when
    GitHub asks; hands sign-in, sudo or a bot check to Dan (cut)."""
    page = rec.page
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        page.wait_for_load_state()
        url = page.url
        if url.startswith(receiver_url):
            return
        if url.startswith(f"{GITHUB}/login/oauth/authorize"):
            button = page.get_by_role("button", name=re.compile(r"^Authorize\b"))
            if button.count():
                page.wait_for_timeout(1200)
                with page.expect_navigation():
                    button.first.click()
                continue
        elif needs_person(page):
            rec.by_hand("GitHub needs you (sign-in, sudo or a check): do it in the browser")
            continue
        page.wait_for_timeout(500)
    raise CheckFailed(f"never got back from GitHub (stuck at {page.url.split('?')[0]})")


def connect(rec: Recorder, target: Target) -> None:
    page = rec.page
    page.goto(target.receiver_url)
    page.locator("#passcode").click()
    rec.by_hand("Type the demo passcode in the browser (don't press Connect)")
    if not page.locator("#passcode").input_value():
        raise CheckFailed("the passcode field is empty")
    with rec.clip():
        page.wait_for_timeout(800)
        page.get_by_role("button", name="Connect GitHub").click()
        follow_github(rec, target.receiver_url)
        page.get_by_text("GitHub account connected.").wait_for(timeout=15_000)
        # The card follows the account.connected event, which the deliverer sends a
        # moment after the redirect.
        until_card(page, "Connected", 30)
        page.wait_for_timeout(2500)


def until_card(page: Any, state: str, seconds: float) -> None:
    def shows() -> bool:
        if card_state(page) == state:
            return True
        page.reload()
        return card_state(page) == state

    wait_for(shows, seconds, 2, f"the card to say {state!r}")


def revoke_on_github(rec: Recorder, target: Target, client_id: str) -> None:
    page = rec.page
    page.goto(f"{GITHUB}/settings/connections/applications/{client_id}")
    while needs_person(page):
        rec.by_hand("GitHub needs you (sign-in or sudo): do it in the browser")
        page.goto(f"{GITHUB}/settings/connections/applications/{client_id}")
    revoke = page.get_by_role("button", name=re.compile(r"Revoke", re.I)).first
    revoke.wait_for(timeout=15_000)
    with rec.clip():
        page.wait_for_timeout(1500)
        if target.local:
            print("rehearsal: not clicking Revoke; the local runner stands in for GitHub")
            revoke.hover()
            page.wait_for_timeout(1000)
        else:
            revoke.click()
            confirm = page.get_by_role("button", name=re.compile(r"I understand", re.I)).first
            confirm.wait_for(timeout=10_000)
            page.wait_for_timeout(800)
            confirm.click()
            page.wait_for_load_state()
            if needs_person(page):
                rec.by_hand("GitHub asks you to confirm: do it in the browser")
            page.wait_for_timeout(1500)
    if target.local:
        target.revoke_stand_in()


def wait_for(check: Callable[[], bool], seconds: float, every: float, what: str) -> None:
    deadline = time.monotonic() + seconds
    while True:
        if check():
            return
        if time.monotonic() > deadline:
            raise CheckFailed(f"timed out waiting for {what}")
        time.sleep(every)


def settle(target: Target, check: Callable[[Facts], None], seconds: float = 60) -> Facts:
    """Re-read the account until ``check`` passes, or fail with its last reason."""
    deadline = time.monotonic() + seconds
    while True:
        facts = _must(target.facts())
        try:
            check(facts)
            return facts
        except CheckFailed:
            if time.monotonic() > deadline:
                raise
        time.sleep(3)


def _must(facts: Facts | None) -> Facts:
    if facts is None:
        raise CheckFailed("the demo account is gone")
    return facts


def _show(label: str, f: Facts) -> None:
    due = f"due in {(f.next_due_at - int(time.time())) // 60} min" if f.next_due_at else "no due"
    tokens = "tokens" if f.has_tokens else "no tokens"
    print(f"  {label}: {f.account_id} {f.state}/{f.reason} v{f.version}, {due}, {tokens}")


def record(target: Target, client_id: str, nudge: bool) -> Manifest:
    from playwright.sync_api import sync_playwright

    target.preflight()
    shutil.rmtree(VIDEOS, ignore_errors=True)
    VIDEOS.mkdir(parents=True)
    timings: dict[str, Any] = {}
    with sync_playwright() as p:
        context = launch(p, record=True)
        rec = Recorder(context)
        try:
            # 1. Connect.
            print("1. connect")
            rec.new_page()
            before = target.facts()
            started = time.time()
            connect(rec, target)
            # The callback's immediate sync sets the next due time from the knob.
            after = settle(target, lambda f: check_connected(before, f, int(time.time())))
            _show("connected", after)
            rec.page.reload()
            shown = last_synced(rec.page)
            connected = after
            rec.close_page()

            # 2. A scheduled sync, with no click. The wait isn't recorded.
            print(f"2. waiting for a scheduled sync (up to {SYNC_WAIT_SECONDS // 60} min)")
            if nudge:
                target.nudge()
            wait_for(
                lambda: synced(connected, target.facts()), SYNC_WAIT_SECONDS, 30, "a scheduled sync"
            )
            minutes = max(1, round((time.time() - started) / 60))
            timings["sync_minutes_after_connect"] = minutes
            after = _must(target.facts())
            _show("synced", after)
            rec.new_page()
            rec.page.goto(target.receiver_url)
            rec.caption(f"About {minutes} minutes later", "a scheduled sync, no click")
            rec.page.goto(target.receiver_url)
            with rec.clip():
                rec.page.wait_for_timeout(1000)

                def moved() -> bool:
                    rec.page.reload()
                    return last_synced(rec.page) != shown

                wait_for(moved, 30, 2, "the page's 'Last synced' to move")
                rec.page.wait_for_timeout(2500)
            synced_facts = after

            # 3. Revoke on GitHub, then the card flips.
            print("3. revoke")
            rec.caption("Revoke the app on GitHub")
            revoke_on_github(rec, target, client_id)
            revoked_at = time.monotonic()
            rec.page.goto(target.receiver_url)
            with rec.clip():
                until_card(rec.page, "Needs re-auth (revoked)", REVOKE_FLIP_SECONDS)
                timings["revoke_flip_seconds"] = round(time.monotonic() - revoked_at)
                rec.page.wait_for_timeout(2500)
            after = settle(target, lambda f: check_revoked(synced_facts, f))
            _show("revoked", after)
            revoked = after

            # 4. Reconnect.
            print("4. reconnect")
            connect(rec, target)
            after = settle(target, lambda f: check_reconnected(revoked, f))
            _show("reconnected", after)
            rec.close_page()
            videos = rec.video_paths()
        finally:
            context.close()
    manifest = Manifest(passed=True, videos=videos, segments=rec.segments, timings=timings)
    validate_segments(manifest.segments, len(manifest.videos))
    MANIFEST.write_text(json.dumps(manifest.to_json(), indent=2))
    return manifest


def sign_in() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = launch(p, record=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(f"{GITHUB}/login")
        input("\n>>> Sign in to GitHub in that window, then press Enter here. ")
        context.close()


def client_id_from_env(path: Path = ROOT / ".env.local") -> str:
    """The GitHub App's Client ID (not a secret), for its own settings page."""
    if path.exists():
        for line in path.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "GITHUB_CLIENT_ID" and value.strip():
                return value.strip().strip("'\"")
    raise CheckFailed(f"no GITHUB_CLIENT_ID in {path.name}: copy .env.local from the main checkout")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sign-in", help="sign in to GitHub in the profile, by hand")
    rec = commands.add_parser("record", help="record, check, then encode")
    where = rec.add_mutually_exclusive_group(required=True)
    where.add_argument("--profile", help="AWS profile: live, against demo")
    where.add_argument("--local", action="store_true", help="rehearse against local.run")
    rec.add_argument("--nudge", action="store_true", help="--local only: don't wait 15 min")
    commands.add_parser("encode", help="re-encode the last passing recording")
    args = parser.parse_args()

    mask: Callable[[str], str] | None = None
    try:
        if args.command == "sign-in":
            sign_in()
            return
        if args.command == "record":
            if args.nudge and not args.local:
                raise CheckFailed("--nudge is for rehearsals only")
            target: Target
            if args.local:
                target = LocalTarget()
            else:
                aws = AwsTarget(args.profile)
                mask = aws.mask
                target = aws
            manifest = record(target, client_id_from_env(), args.nudge)
            print(f"all checks passed: {json.dumps(manifest.timings)}")
        out = encode()
        size = out.stat().st_size / 1_000_000
        print(
            f"{out.relative_to(ROOT)}: {size:.1f} MB; contact sheet: {VIDEOS / 'contact-sheet.png'}"
        )
    except CheckFailed as exc:
        sys.exit(mask_identity(f"check failed, no GIF: {exc}", mask))
    except Exception as exc:
        sys.exit(mask_identity(f"error, no GIF: {type(exc).__name__}: {exc}", mask))


if __name__ == "__main__":
    main()
