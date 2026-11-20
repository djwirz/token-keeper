"""CDK app: Budget, Alerts, CiRoles, DemoReceiver and TokenKeeper (spec §10, ADR 0016).

Environment:
- BUDGET_EMAIL (optional): where budget alerts go. Never committed, never in GitHub.
  The Budget stack exists only when it's set: Dan deploys it by hand, and CI's diff
  and deploy leave it out.
- ALERT_EMAIL (optional): where alarm emails go (ADR 0019). The Alerts stack works the
  same way: it exists only when this is set, and only Dan deploys it.

The stacks are environment-agnostic apart from the region: the account comes from the
credentials at deploy time, so no account ID is written into a template, a diff or a
PR. CI deploys only DemoReceiver and TokenKeeper; Budget, Alerts and CiRoles are
deployed by hand (docs/deploy.md).

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

from infra.stacks.alerts import AlertsStack
from infra.stacks.budget import BudgetStack
from infra.stacks.ci_roles import CiRolesStack
from infra.stacks.demo_receiver import DemoReceiverStack
from infra.stacks.token_keeper import TokenKeeperStack

ROOT = Path(__file__).resolve().parent.parent
LAMBDA_BUILD = ROOT / "build" / "lambda"
REGION = "us-east-1"
# No account: it comes from the credentials at deploy time (ADR 0016).
ENV = cdk.Environment(region=REGION)


def main() -> None:
    if not (LAMBDA_BUILD / "token_keeper" / "handlers" / "callback.py").exists():
        sys.exit("Lambda build missing: run scripts/build_lambda.sh first.")

    app = cdk.App()
    env = ENV
    email = os.environ.get("BUDGET_EMAIL")
    if email:
        BudgetStack(app, "Budget", email=email, env=env)
    alert_email = os.environ.get("ALERT_EMAIL")
    if alert_email:
        AlertsStack(app, "Alerts", email=alert_email, env=env)
    CiRolesStack(app, "CiRoles", env=env)
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
