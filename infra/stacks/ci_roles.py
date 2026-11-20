"""GitHub Actions → AWS through OIDC: no AWS keys anywhere (spec §9, ADR 0016).

Two roles, each trusting exactly one GitHub OIDC subject. The repo uses GitHub's
immutable subjects, which name the owner and repo by name *and* numeric ID
(``repo:<owner>@<owner_id>/<repo>@<repo_id>:...``), so a renamed, deleted or re-created
repo can't inherit the trust. The IDs are public (``gh api repos/<owner>/<repo>``).

- ``DeployRole`` trusts ``<prefix>:environment:demo``. GitHub only issues that subject
  to a job that runs in the ``demo`` environment, whose rules allow only ``main`` and
  require Dan's approval. The role can do nothing but assume CDK's bootstrap roles
  (deploy, file publishing, lookup), which do the actual work.
- ``DiffRole`` trusts ``<prefix>:pull_request`` and can only assume the bootstrap
  lookup role: read-only, though account-wide (ADR 0016). ``cdk diff`` needs no more.

This stack is deployed by hand, once, from an admin session. CI never deploys it, so
CI can't widen its own trust. It uses the L1 OIDC provider (the L2 one needs a
custom-resource Lambda, which is an asset) and the legacy synthesizer, so it deploys
with the operator's credentials and needs no bootstrap roles or bucket.
ARNs are built from ``AWS::AccountId``, never a literal.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import CfnOutput, Duration, LegacyStackSynthesizer, Stack
from aws_cdk import aws_iam as iam
from constructs import Construct

GITHUB_OWNER, GITHUB_OWNER_ID = "djwirz", 15788618
GITHUB_REPO, GITHUB_REPO_ID = "token-keeper", 1463349634
# The prefix GitHub reports for this repo (actions/oidc/customization/sub:
# use_immutable_subject). The name-only form, repo:djwirz/token-keeper:..., never matches.
SUBJECT_PREFIX = f"repo:{GITHUB_OWNER}@{GITHUB_OWNER_ID}/{GITHUB_REPO}@{GITHUB_REPO_ID}"
OIDC_HOST = "token.actions.githubusercontent.com"
AUDIENCE = "sts.amazonaws.com"
DEPLOY_SUBJECT = f"{SUBJECT_PREFIX}:environment:demo"
DIFF_SUBJECT = f"{SUBJECT_PREFIX}:pull_request"
# CDK's default bootstrap qualifier: cdk-<qualifier>-<kind>-role-<account>-<region>.
BOOTSTRAP_QUALIFIER = "hnb659fds"
DEPLOY_ASSUMES = ("deploy", "file-publishing", "lookup")
DIFF_ASSUMES = ("lookup",)


class CiRolesStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs: Any) -> None:
        kwargs.setdefault("synthesizer", LegacyStackSynthesizer())
        super().__init__(scope, construct_id, **kwargs)

        provider = iam.CfnOIDCProvider(
            self,
            "GitHubOidc",
            url=f"https://{OIDC_HOST}",
            client_id_list=[AUDIENCE],
        )

        def bootstrap_role(kind: str) -> str:
            return (
                f"arn:{self.partition}:iam::{self.account}:role/"
                f"cdk-{BOOTSTRAP_QUALIFIER}-{kind}-role-{self.account}-{self.region}"
            )

        def github_role(
            construct_id: str, subject: str, assumes: tuple[str, ...], description: str
        ) -> iam.Role:
            role = iam.Role(
                self,
                construct_id,
                assumed_by=iam.WebIdentityPrincipal(
                    provider.attr_arn,
                    conditions={
                        "StringEquals": {
                            f"{OIDC_HOST}:aud": AUDIENCE,
                            f"{OIDC_HOST}:sub": subject,
                        }
                    },
                ),
                description=description,
                max_session_duration=Duration.hours(1),
            )
            role.add_to_policy(
                iam.PolicyStatement(
                    actions=["sts:AssumeRole"],
                    resources=[bootstrap_role(kind) for kind in assumes],
                )
            )
            return role

        deploy = github_role(
            "DeployRole",
            DEPLOY_SUBJECT,
            DEPLOY_ASSUMES,
            "token-keeper: cdk deploy from main through the demo environment",
        )
        diff = github_role(
            "DiffRole",
            DIFF_SUBJECT,
            DIFF_ASSUMES,
            "token-keeper: read-only cdk diff on pull requests",
        )
        # Read by the operator to set the GitHub secrets (docs/deploy.md); never printed.
        CfnOutput(self, "DeployRoleArn", value=deploy.role_arn)
        CfnOutput(self, "DiffRoleArn", value=diff.role_arn)
