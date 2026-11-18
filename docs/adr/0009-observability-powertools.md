# 9. Observability with Powertools for AWS Lambda

Date: 2026-11-18
Status: Proposed

## Context
Depth 3 requires observability. The budget is $5 a month, and the time is one to two weeks.

## Options considered
1. **Powertools for AWS Lambda (Python):** structured logs, EMF metrics and X-Ray tracing, plus one CloudWatch dashboard and three alarms to SNS email.
2. **OpenTelemetry export** to a vendor's free tier (e.g. Grafana Cloud). Vendor-neutral, more setup, and another account.
3. **Logs and alarms only.** Leaner, but it loses metrics and tracing.

## Decision
Option 1. The alarms are: either DLQ non-empty, elevated refresh-failure rate, and planner missing for 45 minutes. A log scrubber and a test keep token-shaped strings out of logs. `docs/runbook.md` covers each alarm.

## Consequences
- AWS-native and within free allowances at demo volume.
- Traces span SQS hops through propagated correlation ids.
- Moving to OpenTelemetry later is possible (Powertools and ADOT coexist), but not planned.
