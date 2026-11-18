# 6. AWS CDK (Python) for infrastructure as code

Date: 2026-11-18
Status: Accepted

## Context
The roadmap originally named Terraform as a skill this project should prove. During shaping, Dan questioned that: they are an application developer, not a DevOps specialist, and want IaC that fits a modern, code-first workflow. Requirements: infrastructure reviewed in PRs, deployable from CI with OIDC, least-privilege IAM, testable, and cheap to run.

## Options considered
1. **Terraform.** Cloud-neutral and widely asked for. It's another language (HCL), and it needs a state backend we'd have to manage.
2. **AWS CDK in Python.** Same language as the app. Typed constructs and `grant_*` helpers produce least-privilege IAM. State lives in CloudFormation, so there's nothing to host. `cdk watch` hot-swaps Lambda code for a fast dev loop, and assertion tests cover infrastructure.
3. **AWS SAM.** Serverless-focused YAML with a good local loop (`sam local`, `sam sync --watch`). Less expressive for IAM and alarms, and a thinner portfolio signal.
4. **Pulumi (Python).** Infra in Python across clouds, but it needs a Pulumi service account or a self-hosted state backend.

## Decision
Option 2: AWS CDK in Python, with stacks `Budget` (deployed first), `TokenKeeper` and `DemoReceiver`.

## Consequences
- One language across the repo, and IAM grants are readable and unit-tested.
- AWS lock-in at the IaC layer. Acceptable, since the architecture is AWS-native anyway.
- **The roadmap's "Terraform" proof point is dropped** for this project. The build-plan roadmap should say "IaC (CDK, Python)". If Terraform is still wanted for the job search, a later project can carry it.
- CloudFormation's slower deploys and occasional stuck rollbacks are the known pain. `cdk watch` covers inner-loop speed.
