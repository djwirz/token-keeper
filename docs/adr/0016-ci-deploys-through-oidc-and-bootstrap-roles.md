# 16. CI deploys through OIDC roles that only assume CDK's bootstrap roles

Date: 2026-11-19
Status: Accepted

## Context
Spec §9 says CI reaches AWS through GitHub Actions OIDC, with no stored keys. There's a deploy role trusting the `demo` environment's subject and a read-only role for `cdk diff` on pull requests. Slice 4 is the first deploy. Several choices it has to make are expensive to change once the account has roles and stacks in it:

- **What the CI roles may do.** `cdk deploy` needs CloudFormation, an S3 asset bucket and a role CloudFormation can pass. Granting all of that to the GitHub role directly makes it as broad as the stacks it deploys.
- **Who deploys the stacks that hold trust and money settings.** The OIDC roles and the budget are not app code. A deploy role that can change the stack defining its own trust can widen that trust. The budget's alert address is Dan's email, which shouldn't sit in GitHub.
- **Account IDs.** The repo is public, and CI's diff output is posted to PRs. A stack with an explicit account bakes the ID into every ARN in its template, and so into every diff.
- **Order.** Spec §3 wants the budget to exist before anything else is deployed. A normal CDK stack can't deploy before `cdk bootstrap`, and bootstrapping is itself a deploy.

## Options considered
1. **CI roles with direct CloudFormation, S3 and PassRole permissions.** This is common in examples, but the policy has to grow with every resource type the stacks use.
2. **CI roles that can only `sts:AssumeRole` into CDK's bootstrap roles** (deploy, file publishing, lookup). This is CDK's own model: the bootstrap deploy role drives CloudFormation, and CloudFormation acts through its execution role.
3. **Long-lived access keys in GitHub secrets.** Ruled out by spec §9.

For the trust and money stacks: let CI deploy every stack, or deploy `Budget` and `CiRoles` by hand from an admin session and have CI deploy only `DemoReceiver` and `TokenKeeper`.

## Decision
Option 2, plus these rules:

- **`CiRoles` stack:**
  - Holds the GitHub OIDC provider (the L1 resource: the L2 one needs a custom-resource Lambda) and two roles, each with a one-hour session.
  - `DeployRole` trusts exactly `repo:djwirz/token-keeper:environment:demo` with audience `sts.amazonaws.com`. It may assume the bootstrap deploy, file-publishing and lookup roles, nothing else.
  - `DiffRole` trusts exactly `repo:djwirz/token-keeper:pull_request`, and may assume only the bootstrap lookup role, which is read-only.
  - The `demo` GitHub environment requires Dan's approval and allows only `main`. GitHub only issues the environment subject to a job in that environment, so those rules are what stand between a branch and a deploy.
- **Hand-deployed:** `Budget` and `CiRoles` are deployed once, by hand. CI's diff and deploy name `DemoReceiver TokenKeeper` explicitly. `Budget` exists in the app only when `BUDGET_EMAIL` is set, so CI can't even synth it with a real address.
- **Bootstrapless synthesis:** `Budget` and `CiRoles` have no assets, so they use CDK's bootstrapless synthesizer and deploy with the operator's own credentials. The budget really is the first thing in the account, ahead of `cdk bootstrap`.
- **Environment-agnostic stacks:** the region is fixed (`us-east-1`), and the account comes from the credentials at deploy time. Templates hold `${AWS::AccountId}`, never a literal. A test fails if any template contains a 12-digit number. CI masks the account ID in logs, scrubs anything account-shaped from the posted diff, and keeps role ARNs in GitHub secrets.
- **Fork PRs** don't get the diff job (it's skipped unless the PR's head repo is this one). GitHub doesn't give them an OIDC token anyway.

## Consequences
- The CI roles' own policies stay two lines long, however the stacks grow.
- **A stated limitation:** CDK's bootstrap gives CloudFormation's execution role `AdministratorAccess` by default. So a deploy that passes Dan's approval can create anything in the account. Narrowing it (`cdk bootstrap --cloudformation-execution-policies`) means listing every service the stacks use and re-bootstrapping whenever that changes. At demo scale, with one approver, that's left out on purpose, and noted for the case study.
- **The first deploy breaks spec §11.3's "only from `main`":** the app stacks' first deploy runs from Dan's laptop, because the deploy workflow only exists once slice 4 merges and the first deploy needs fix-and-retry. From the merge on, only CI deploys the app stacks.
- **Changing CI's trust takes a hand deploy:** a new environment, or GitHub's newer immutable-ID subjects. That's the point: trust changes happen at an admin's desk, not through the pipeline they control.
- The diff job needs the deployed stacks and the `AWS_DIFF_ROLE_ARN` secret. Without them it fails loudly rather than posting an empty diff. Spec §3 still lists it first to cut.
