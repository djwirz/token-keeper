"""CDK app. Synth only until slice 4 (spec §12).

Environment:
- BUDGET_EMAIL (required): where budget alerts go. Never committed.

The stacks find each other's URLs (and the GitHub client ID) through SSM parameters
at runtime, not through CloudFormation exports, so they deploy in any order (ADR 0014).

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
    DemoReceiverStack(
        app,
        "DemoReceiver",
        # One Code instance per stack (CDK refuses to share one); same files and hash.
        code=lambda_.Code.from_asset(str(LAMBDA_BUILD)),
        env=env,
    )
    TokenKeeperStack(
        app,
        "TokenKeeper",
        code=lambda_.Code.from_asset(str(LAMBDA_BUILD)),
        env=env,
    )
    cdk.Tags.of(app).add("project", "token-keeper")
    app.synth()


if __name__ == "__main__":
    main()
