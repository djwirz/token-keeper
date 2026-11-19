"""CDK app. Synth only until slice 4 (spec §12).

Environment:
- BUDGET_EMAIL (required): where budget alerts go. Never committed.
- TOKEN_KEEPER_CONNECT_URL (optional): Token Keeper's /connect URL, given to the
  receiver. It can't be a cross-stack reference, because Token Keeper already depends
  on the receiver's URL; it defaults to a placeholder until the first deploy (slice 4).
- GITHUB_CLIENT_ID (optional): the GitHub App's client ID (not a secret).

Lambda code comes from build/lambda; run scripts/build_lambda.sh first.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_lambda as lambda_

from infra.stacks.budget import BudgetStack
from infra.stacks.demo_receiver import DemoReceiverStack
from infra.stacks.token_keeper import TokenKeeperStack

ROOT = Path(__file__).resolve().parent.parent
LAMBDA_BUILD = ROOT / "build" / "lambda"
PLACEHOLDER_CONNECT_URL = "https://token-keeper.invalid/connect"
PLACEHOLDER_CLIENT_ID = "unset"


def main() -> None:
    email = os.environ.get("BUDGET_EMAIL")
    if not email:
        sys.exit("Set BUDGET_EMAIL (budget alerts go there).")
    if not (LAMBDA_BUILD / "token_keeper" / "handlers" / "callback.py").exists():
        sys.exit("Lambda build missing: run scripts/build_lambda.sh first.")

    app = cdk.App()
    env = cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    )
    BudgetStack(app, "Budget", email=email, env=env)
    receiver = DemoReceiverStack(
        app,
        "DemoReceiver",
        # One Code instance per stack (CDK refuses to share one); same files and hash.
        code=lambda_.Code.from_asset(str(LAMBDA_BUILD)),
        token_keeper_connect_url=os.environ.get(
            "TOKEN_KEEPER_CONNECT_URL", PLACEHOLDER_CONNECT_URL
        ),
        env=env,
    )
    TokenKeeperStack(
        app,
        "TokenKeeper",
        code=lambda_.Code.from_asset(str(LAMBDA_BUILD)),
        receiver_url=receiver.public_url,
        github_client_id=os.environ.get("GITHUB_CLIENT_ID", PLACEHOLDER_CLIENT_ID),
        env=env,
    )
    cdk.Tags.of(app).add("project", "token-keeper")
    app.synth()


if __name__ == "__main__":
    main()
