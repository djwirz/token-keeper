"""EMF metrics (spec §8, ADR 0009, ADR 0019).

One namespace, one dimension (``service``), nothing else: every extra dimension value
is billed as its own metric, so failure reasons go in logs instead. Powertools' cold-
start metric stays off (it's only emitted when asked for).

Each handler is wrapped in ``flushed``, which prints the invocation's metrics as one
EMF line on stdout; CloudWatch Logs turns it into metrics, so no role needs
``cloudwatch:PutMetricData``. An invocation that counted nothing prints nothing
(Powertools would warn instead).

The names live here so the handlers, the tests and the CDK dashboard and alarms share
one spelling: a rename can't leave an alarm watching a metric nobody emits.
"""

from __future__ import annotations

import functools
from collections.abc import Callable

from aws_lambda_powertools import Metrics
from aws_lambda_powertools.metrics import MetricUnit

from token_keeper.log import SERVICE

NAMESPACE = "TokenKeeper"

REFRESH_SUCCEEDED = "RefreshSucceeded"
# Only a refresh GitHub rejected (bad_refresh_token): a transient token-endpoint error
# isn't counted, so a GitHub outage raises no alarm (ADR 0015, ADR 0019).
REFRESH_FAILED = "RefreshFailed"
SYNC_SUCCEEDED = "SyncSucceeded"
SYNC_FAILED = "SyncFailed"
DELIVERY_SUCCEEDED = "DeliverySucceeded"
DELIVERY_FAILED = "DeliveryFailed"
DELIVERY_LATENCY = "DeliveryLatency"
ACCOUNTS_DUE = "AccountsDue"
# A rejected connect link or callback (spec §7).
CONNECT_REJECTED = "ConnectRejected"
REVOCATION_RECEIVED = "RevocationReceived"
# A GitHub App delivery that failed its signature check or was malformed (spec §7).
GITHUB_WEBHOOK_REJECTED = "GitHubWebhookRejected"

COUNTS = (
    REFRESH_SUCCEEDED,
    REFRESH_FAILED,
    SYNC_SUCCEEDED,
    SYNC_FAILED,
    DELIVERY_SUCCEEDED,
    DELIVERY_FAILED,
    ACCOUNTS_DUE,
    CONNECT_REJECTED,
    REVOCATION_RECEIVED,
    GITHUB_WEBHOOK_REJECTED,
)
ALL = (*COUNTS, DELIVERY_LATENCY)

_metrics = Metrics(namespace=NAMESPACE, service=SERVICE)


def count(name: str, value: int = 1) -> None:
    _metrics.add_metric(name=name, unit=MetricUnit.Count, value=value)


def milliseconds(name: str, value: float) -> None:
    _metrics.add_metric(name=name, unit=MetricUnit.Milliseconds, value=value)


def flush() -> None:
    if _metrics.metric_set:
        _metrics.flush_metrics()


def flushed[**P, R](handler: Callable[P, R]) -> Callable[P, R]:
    """Print what this invocation counted, even when the handler raises."""

    @functools.wraps(handler)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return handler(*args, **kwargs)
        finally:
            flush()

    return wrapper
