# Case-study notes

Raw material for the write-up, not the write-up itself. Every claim links to the PR or ADR it comes from. Merged PRs are the project's record (CLAUDE.md).

PR links: [1](https://github.com/djwirz/token-keeper/pull/1) spec · [2](https://github.com/djwirz/token-keeper/pull/2) local foundation · [3](https://github.com/djwirz/token-keeper/pull/3) connect → callback · [4](https://github.com/djwirz/token-keeper/pull/4) sync and refresh · [5](https://github.com/djwirz/token-keeper/pull/5) first deploy · [6](https://github.com/djwirz/token-keeper/pull/6) revocation webhook · [7](https://github.com/djwirz/token-keeper/pull/7) its live proof · [8](https://github.com/djwirz/token-keeper/pull/8) a drift fix · [9](https://github.com/djwirz/token-keeper/pull/9) observability · [10](https://github.com/djwirz/token-keeper/pull/10) its live proof.

## What it is, in one paragraph
A connector service for one provider (GitHub). It runs the OAuth consent, keeps each user's tokens encrypted and alive, syncs a small public snapshot on a schedule, and reports each account's state to a main app through one signed webhook. Its job is to prove that the refresh lifecycle survives concurrency, that tokens stay safe, and that it's deployed and operable. The Instagram angle: GitHub App user tokens have the same shape as Instagram's long-lived tokens, plus single-use rotation ([ADR 0002](adr/0002-github-app-with-expiring-user-tokens.md)).

## Timeline
About five days from spec to a deployed, alarmed system, against an appetite of one to two weeks (spec §3):
- **2026-11-18:** spec and ADRs merged (PR 1).
- **2026-11-19 and 20:** three local-only slices. They included real refresh-token rotations against Dan's own GitHub account, run through the local runner (PRs 2–4).
- **2026-11-20:**
  - the first deploy, through hand-deployed budget and CI-role stacks, then OIDC (PR 5);
  - the revocation webhook, proven live the same day (PRs 6–7).
- **2026-11-21 and 22:** observability, with every alarm forced live (PRs 9–10).
- **Slice 7:** the cadence knob, these notes, then the GIF.

## What was proven live, not just tested
- **IAM narrowing:** `dynamodb:LeadingKeys` and `dynamodb:Attributes` conditions, proven with real calls (`prove_iam.py --live`), not just the policy simulator. The simulator alone can't catch a condition DynamoDB never evaluates ([ADR 0017](adr/0017-dynamodb-condition-keys.md), PR 5).
- **Revocation:** revoke in GitHub's settings, the card flips within seconds, redeliveries are ignored, and a reconnect flips it back ([ADR 0018](adr/0018-revocation-webhook-redeliveries-and-trust.md), "Proven live").
- **Alarms:** all three alarms were fired by their forcing method and recovered, with the emails. A DLQ redrive worked, and real GitHub answered `bad_refresh_token` as expected ([ADR 0019](adr/0019-observability-as-built.md), "Proven live").
- **Rotation:** real refreshes, locally and on `demo`.

## Trade-offs left out on purpose
Each one is written down where it was decided, so the write-up can say "chosen, not missed":
- **The CDK bootstrap's execution role has `AdministratorAccess`.** A deploy that passes Dan's approval can create anything. Narrowing it means re-bootstrapping whenever the stacks use a new service ([ADR 0016](adr/0016-ci-deploys-through-oidc-and-bootstrap-roles.md)).
- **The `due` index partitions by state.** That's a hot partition far beyond demo scale ([ADR 0004](adr/0004-planner-sqs-worker-schedule.md)).
- **The KMS key keeps CDK's default key policy,** so account administrators can still decrypt (spec §9, [ADR 0003](adr/0003-field-level-kms-encryption.md)).
- **Lost rotation.** If GitHub rotates the pair and the response is lost, the account needs re-auth. GitHub documents no grace period (spec §7).
- **Not tested live: does a quiet DLQ stop reporting?** That needs a message left in a DLQ for six hours. It was deferred (ADR 0019, "Still open"; PR 9 review).
- **The dashboard is checked through the API only.** The console needs a passkey, which the browser Claude drives can't complete (ADR 0019).
- **The cadence knob drifts while it's set.** That's accepted for a few minutes of recording ([ADR 0020](adr/0020-demo-cadence-knob-in-ssm.md)).

## How AI was used
Claude Code did most of the typing. Dan made every decision and did everything that needed their identity: GitHub sign-ins, app settings, AWS account setup, approvals, and revoking.

The workflow that held up:
- **Spec first, then one thin slice per PR,** each slice planned in plan mode and approved before any code.
- **An ADR for every decision that's expensive to reverse,** written as Accepted in the PR that adds it, and never rewritten after merging.
- **A fresh-eyes review of every slice PR** by a separate Claude Code session that only reports. The main session applies the fixes. (Two sessions once fixed the same findings in parallel, PR 5, which is why the review only reports.)
- **Mutation checks:** break the code on purpose and confirm a test fails. This is what caught most of the "tests that claim more than they check" below.
- **Live drills after each deploy.** Every hand change to AWS waits for Dan's "go", and every result is checked against AWS itself, drift included.
- **Mistakes go in the PR description,** under "How AI was used".

## Where AI was wrong
Collected from PRs 1–10 and the ADRs. They're grouped by pattern, because the patterns are the story.

### Tests that claimed more than they checked
The most common mistake, and the reason mutation checks became routine.
- **The token-in-logs test (PR 3).** It said it turned every logger to DEBUG, but third-party loggers stayed at WARNING. At DEBUG, botocore would have logged tokens base64-encoded inside KMS request bodies, which the scrubber didn't match. Nothing leaked, because the level is pinned in code. The test was fixed, and the scrubber now matches base64.
- **A stale-read concurrency test with nothing stale in it (PR 4).** The review's mutation (use tokens read before the lease) passed all 25 tests until the test was rewritten.
- **A local runner test that passed while every sync failed** on an unmocked endpoint (PR 4).
- **Tests that pinned the project's own constant instead of what the real system sends:**
  - the OIDC subject, so every CI assume was denied in production (PR 5);
  - a synthesizer test that read the template, not the manifest the CLI reads (PR 5);
  - an alarm ARN in the form only the test app writes (PR 9).
- **A "no wildcard IAM" assertion** that missed a wildcard inside an ARN (PR 2).

### Confident claims about other systems that were wrong
- **"An OAuth App leaves nothing to keep alive."** That had been out of date since GitHub's August 2026 change. ADR 0002 now rests on permissions and the revocation webhook instead (PR 1).
- **An OIDC trust scoped to both a branch and an environment.** GitHub's subject carries no branch when a job uses an environment (PR 1 review).
- **"IAM Identity Center and Organizations are free."** On the free account plan, creating an Organization forces the paid plan. Dan caught it before clicking (PR 5).
- **"GitHub documents `action: revoked`."** Only Octokit's schema does (PR 6).
- **"A CI deploy puts back a property changed by hand."** It doesn't. This was only checked after the deploy had run (PR 8).
- **The DLQ redrive.** A review called the destination-less command checked and correct. It fails for a message that never dead-lettered (PR 10).

### Bugs only a real browser or a real deploy showed
- **The receiver's CSP `form-action` blocked the redirect to Token Keeper, then the next hop to GitHub.** Twice, and the first PR description said it "lands on a 404" (PRs 2–3).
- **GitHub's events feed isn't strictly newest first.** The test fakes were generated in order (PR 4).
- **The first real revocation was unsigned,** because the App's webhook secret wasn't saved. The plan's "ping" check had fired before the route existed (PR 7).

### Docs and plans written before the run
- **Runbook timings were optimistic:** the DLQ recovery and PlannerMissing took longer than written (PR 10).
- **A plan wrote `--account-id` for a positional argument** (PR 10).
- **A plan assumed the built-in browser could open the CloudWatch console** (PR 10).
- **A residual risk was put at "a few milliseconds";** seconds is the honest estimate (PR 1).

### Design over-reach that Dan cut
- A circuit breaker and a time-based `needs_reauth` for day-long GitHub outages (PR 1).
- Deploy-first slicing; local-first turned out to suit a project whose proof is mostly Python logic (PR 1).
- A status-only service that would show nothing; this led to snapshot sync, [ADR 0008](adr/0008-sync-snapshots-not-token-vending.md) (PR 1).

### Smaller ones worth one line each
- The local runner printed OAuth `code` and `state` in its access log (PR 3).
- A cross-stack export chosen only to silence a synth warning; replaced by SSM, [ADR 0014](adr/0014-stacks-share-urls-through-ssm.md) (PR 3).
- A redelivery guard that remembered only the last delivery ID, which a second revoke and reconnect cycle would have broken (PR 6 review).
- Unsplit fake secrets tripped gitleaks after a push (PR 2).
- Checking too early: two "missing" emails had arrived a minute later (PR 10).
- The wrong pronoun for Dan in the first spec draft (PR 1).

## Numbers to use carefully
Keep them as words, as the repo does: "about five days", "about a dollar a month for the KMS key", "within seconds", "about an hour". The precise timings in the ADRs' "Proven live" sections come from single runs.
