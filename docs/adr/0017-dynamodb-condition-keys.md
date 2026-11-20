# 17. DynamoDB condition keys narrow the table grants, proven in a deploy

Date: 2026-11-20
Status: Accepted

## Context
Spec §9 asks for least privilege, one role per Lambda. Slices 2 and 3 granted DynamoDB actions on the whole table and put off narrowing them until a deploy could prove it (PR 3 and PR 4, "Known limitations"):
- **Item-level:** `dynamodb:LeadingKeys` could limit each role to the partition-key prefixes it really writes (ADR 0013's `ACCOUNT#`, `STATE#`, `LINK#`, `GHUSER#`).
- **Attribute-level:** `dynamodb:Attributes` could hold the planner to the fields its code changes (`next_due_at`, `version`), so it could never touch a token, an expiry or the lease.

Neither can be proven locally: moto doesn't evaluate IAM. They're also easy to get wrong in a way no test notices. Both use `ForAllValues`, which is true when the request carries no such key at all. A condition DynamoDB never feeds is a condition that never denies, and a policy simulator only evaluates the context it's given, so it can't show that.

## Options considered
1. **Keep whole-table grants.** Simple, and honest in the docs, but the planner, which never needs a token, could overwrite one.
2. **Conditions, checked with IAM's policy simulator.** Read-only and repeatable, but for the reason above it can't prove DynamoDB actually enforces them.
3. **Conditions, checked with the simulator, the real deployed flows, and real requests under copies of the deployed policies.** `scripts/prove_iam.py --live` creates one temporary role per Lambda role with a copy of its inline policies. It sends real DynamoDB requests against `iam-probe` keys that don't exist, with conditions that can't hold, so nothing is written. Then it deletes the roles. IAM is evaluated before the condition expression, so `ConditionalCheckFailed` means allowed and `AccessDenied` means denied.

## Decision
Option 3, with these conditions. Each was kept because it passed all three checks:

| Role | Actions | Condition |
|---|---|---|
| connect | `PutItem` | `LeadingKeys` like `STATE#*` |
| worker | `UpdateItem`, `PutItem` | `LeadingKeys` like `ACCOUNT#*` |
| deliverer | `Query` | `LeadingKeys` like `ACCOUNT#*` |
| planner | `UpdateItem` | `LeadingKeys` like `ACCOUNT#*`; `Attributes` ⊆ `PK, SK, next_due_at, version, state`; `ReturnValues` = `NONE` if present |
| planner | `Query` on the `due` index | none: there the leading key is `state` |
| callback | `GetItem`, `DeleteItem`, `PutItem`, `UpdateItem` | none: it writes all four prefixes, so a condition would exclude nothing |

None were dropped. What the first deploy (2026-11-20) showed:
- **The real flows passed with every condition on:** two connects (one taking the reconnect path), their syncs, a scheduled planner run that pushed a due account, and a real refresh-token rotation. No log had an `AccessDenied`.
- **Simulate and live agree on all 16 cases:** each handler's real request shape is allowed. Off-pattern requests are denied: connect writing `ACCOUNT#`, the deliverer querying `LINK#`, the worker writing `STATE#` or `GHUSER#`, and the planner setting `access_token_ct` or `lease_owner`, asking for `ALL_NEW`, or touching a `LINK#` row.
- **`state` has to be in the planner's list.** It's named only in the planner's condition expression, yet with it removed, the planner's real request is denied. So DynamoDB counts every name a request uses, not only the ones it writes. This also shows the attribute condition is genuinely enforced, not vacuously true.

## Consequences
- The planner can no longer write a token, an expiry, the lease or `state` itself. `state` is in the list, but its code never sets it.
- The worker, connect and deliverer can't touch rows outside their prefix. The callback stays table-wide.
- **Tied to the code:** a CDK assertion test pins each condition exactly. Another test reads the attribute names from the planner's real `push_due` request and requires them to equal the IAM list, so a new field in that request fails CI instead of failing in AWS.
- **A known gap:** the `ForAllValues` empty-set case. Every request these roles make is a keyed item operation, which carries `LeadingKeys`, and the live probe shows DynamoDB sends both keys. A future `Scan` or `BatchWriteItem` on these roles would need its own check before it's trusted.
- **Rerun on IAM changes:** `prove_iam.py` is cheap to rerun (simulate is read-only, live creates and deletes its roles). Rerun it after any change to these grants.
