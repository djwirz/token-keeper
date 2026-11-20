"""Write the demo's SSM parameters (spec §9, docs/deploy.md). Run by Dan, never by CI.

    uv run python scripts/put_demo_secrets.py --profile token-keeper
    uv run python scripts/put_demo_secrets.py --profile token-keeper --show-passcode
    uv run python scripts/put_demo_secrets.py --profile token-keeper --github-webhook-secret

- The GitHub App's client ID and secret come from the main checkout's ``.env.local``
  (the same App serves localhost and the deployed callback).
- The webhook secret, connect-link secret and demo passcode are generated fresh, in the
  formats ``local/run.py`` uses. Each one shared by both stacks is written from a single
  value to both names.
- No value is ever printed, except the passcode with ``--show-passcode``, which only
  reads it back. Existing parameters are left alone unless ``--overwrite`` is given;
  then both sides of a shared secret are written back to back. SSM has no transaction,
  so if a write fails between them the script says so: rerun with ``--overwrite``, which
  generates and writes a fresh pair.
- ``--github-webhook-secret`` (slice 5) writes only the GitHub App's webhook secret:
  32 random bytes as hex, generated here. It goes to SSM and onto the clipboard
  (``pbcopy``) for pasting into the App's settings, and is never printed or written to
  disk. It refuses if the parameter exists, unless ``--overwrite`` is given; then the
  App's setting must be pasted again, or every delivery fails its signature check.
"""

from __future__ import annotations

import argparse
import base64
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.stacks.demo_receiver import SECRET_PARAMS as RECEIVER
from infra.stacks.shared import GITHUB_CLIENT_ID_PARAM
from infra.stacks.token_keeper import SECRET_PARAMS as TOKEN_KEEPER

REGION = "us-east-1"


def main_checkout_env() -> dict[str, str]:
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    path = Path(common).parent / ".env.local"
    if not path.exists():
        sys.exit(f"{path} not found")
    values = {}
    for line in path.read_text().splitlines():
        name, sep, value = line.partition("=")
        if sep and not name.startswith("#"):
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]  # KEY="value" is common in .env files
            values[name.strip()] = value
    return values


def exists(ssm: Any, name: str) -> bool:
    try:
        ssm.get_parameter(Name=name)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ParameterNotFound":
            raise
        return False
    return True


def to_clipboard(value: str) -> None:
    # Through stdin, so the value never appears in a process list.
    subprocess.run(["pbcopy"], input=value, text=True, check=True)  # noqa: S607


def put_github_webhook_secret(ssm: Any, *, overwrite: bool) -> None:
    name = TOKEN_KEEPER["GITHUB_WEBHOOK_SECRET_PARAM"]
    if exists(ssm, name) and not overwrite:
        sys.exit(f"Already set (nothing written; use --overwrite): {name}")
    value = secrets.token_hex(32)
    try:
        ssm.put_parameter(Name=name, Value=value, Type="SecureString", Overwrite=True)
    except ClientError as exc:
        sys.exit(f"failed writing {name}: {exc.response['Error']['Code']}")
    to_clipboard(value)
    print(f"wrote {name} (SecureString)")
    print(
        "The secret is on your clipboard. Paste it into the GitHub App's settings, "
        "Webhook secret, and save. Then copy something else to clear it."
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--show-passcode", action="store_true")
    parser.add_argument("--github-webhook-secret", action="store_true")
    args = parser.parse_args()
    ssm = boto3.Session(profile_name=args.profile, region_name=REGION).client("ssm")

    if args.github_webhook_secret:
        put_github_webhook_secret(ssm, overwrite=args.overwrite)
        return

    if args.show_passcode:
        name = RECEIVER["DEMO_PASSCODE_PARAM"]
        print(ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"])
        return

    env = main_checkout_env()
    client_id, client_secret = env.get("GITHUB_CLIENT_ID"), env.get("GITHUB_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET must be set in .env.local")
    if client_id.isdigit():
        sys.exit("GITHUB_CLIENT_ID is all digits: that's the App ID, not the client ID")

    webhook = "whsec_" + base64.b64encode(secrets.token_bytes(32)).decode()
    connect_link = secrets.token_urlsafe(32)
    # Each shared secret's two names are adjacent, so a failure splits at most one pair.
    secure = {
        TOKEN_KEEPER["GITHUB_CLIENT_SECRET_PARAM"]: client_secret,
        TOKEN_KEEPER["CONNECT_LINK_SECRET_PARAM"]: connect_link,
        RECEIVER["CONNECT_LINK_SECRET_PARAM"]: connect_link,
        TOKEN_KEEPER["WEBHOOK_SECRET_PARAM"]: webhook,
        RECEIVER["WEBHOOK_SECRET_PARAM"]: webhook,
        RECEIVER["DEMO_PASSCODE_PARAM"]: secrets.token_urlsafe(16),
    }
    names = [*secure, GITHUB_CLIENT_ID_PARAM]

    existing = [name for name in names if exists(ssm, name)]
    if existing and not args.overwrite:
        sys.exit("Already set (nothing written; use --overwrite):\n  " + "\n  ".join(existing))

    for name, value in secure.items():
        try:
            ssm.put_parameter(Name=name, Value=value, Type="SecureString", Overwrite=True)
        except ClientError as exc:
            # The error names the parameter, never its value.
            sys.exit(
                f"failed writing {name}: {exc.response['Error']['Code']}. A shared secret "
                "may now differ between the stacks: rerun with --overwrite."
            )
        print(f"wrote {name} (SecureString)")
    ssm.put_parameter(Name=GITHUB_CLIENT_ID_PARAM, Value=client_id, Type="String", Overwrite=True)
    print(f"wrote {GITHUB_CLIENT_ID_PARAM} (String)")
    print("Done. The passcode: rerun with --show-passcode when you need it.")


if __name__ == "__main__":
    main()
