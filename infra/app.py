"""CDK app. Synth only until slice 4 (spec §12).

Environment:
- BUDGET_EMAIL (required): where budget alerts go. Never committed.
- TOKEN_KEEPER_CONNECT_URL (optional): Token Keeper's /connect URL. Until the
  TokenKeeper stack exists (slice 4) it defaults to a placeholder.

Lambda code comes from build/receiver; run scripts/build_lambda.sh first.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_lambda as lambda_

from infra.stacks.budget import BudgetStack
from infra.stacks.demo_receiver import DemoReceiverStack

ROOT = Path(__file__).resolve().parent.parent
RECEIVER_BUILD = ROOT / "build" / "receiver"
PLACEHOLDER_CONNECT_URL = "https://token-keeper.invalid/connect"


def main() -> None:
    email = os.environ.get("BUDGET_EMAIL")
    if not email:
        sys.exit("Set BUDGET_EMAIL (budget alerts go there).")
    if not (RECEIVER_BUILD / "receiver" / "handler.py").exists():
        sys.exit("Lambda build missing: run scripts/build_lambda.sh first.")

    app = cdk.App()
    env = cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    )
    BudgetStack(app, "Budget", email=email, env=env)
    DemoReceiverStack(
        app,
        "DemoReceiver",
        code=lambda_.Code.from_asset(str(RECEIVER_BUILD)),
        token_keeper_connect_url=os.environ.get(
            "TOKEN_KEEPER_CONNECT_URL", PLACEHOLDER_CONNECT_URL
        ),
        env=env,
    )
    cdk.Tags.of(app).add("project", "token-keeper")
    app.synth()


if __name__ == "__main__":
    main()
