# 9. Observability with Powertools for AWS Lambda

Date: 2026-11-18
Status: Accepted

## Context
Depth 3 requires observability. The budget is $5 a month, and the time is one to two weeks.

## Options considered
1. **Powertools for AWS Lambda (Python):** structured logs, EMF metrics and X-Ray tracing, plus one CloudWatch dashboard and three alarms to SNS email.
2. **OpenTelemetry export** to a vendor's free tier (e.g. Grafana Cloud). Vendor-neutral, more setup, and another account.
3. **Logs and alarms only.** Leaner, but it loses metrics and tracing.

## Decision
Option 1, kept cheap:
- **Metrics:** about ten custom metrics with a single `service` dimension. Failure reasons go in logs, not dimensions, because every dimension combination is billed as its own metric. Powertools' cold-start metric is off.
- **Logs:** log groups keep two weeks.
- **Alarms:** a DLQ has messages; `RefreshFailed` is one or more in an hour; the planner hasn't been invoked for 45 minutes. Each has a concrete way to force it (spec §8).
- **Hygiene:** a log scrubber and a test keep token-shaped strings out of logs. `docs/runbook.md` covers each alarm.

## Consequences
- AWS-native. Custom metrics are the one real cost: a few are emitted around the clock (the planner's), most only when something happens, so the estimate is a dollar or two a month at worst, inside the budget with the KMS key.
- X-Ray shows each SQS hop as a linked trace, not one continuous trace. The `correlation_id` in logs is what ties a whole flow together.
- Moving to OpenTelemetry later is possible (Powertools and ADOT coexist), but not planned.
