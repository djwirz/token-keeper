"""The $5/month AWS budget with email alerts at 50% and 100% (spec §3, §8).

Deployed before anything else (spec §12, slice 4), even before ``cdk bootstrap``: it
has no assets, so the bootstrapless synthesizer lets the CLI deploy it with the
operator's own credentials. Deployed by hand only; CI never sees the email (ADR 0016).
"""

from __future__ import annotations

from typing import Any

from aws_cdk import BootstraplessSynthesizer, Stack
from aws_cdk import aws_budgets as budgets
from constructs import Construct

MONTHLY_LIMIT_USD = 5
ALERT_PERCENTAGES = (50, 100)


class BudgetStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, email: str, **kwargs: Any) -> None:
        kwargs.setdefault("synthesizer", BootstraplessSynthesizer())
        super().__init__(scope, construct_id, **kwargs)
        budgets.CfnBudget(
            self,
            "MonthlyBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name="token-keeper-monthly",
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=MONTHLY_LIMIT_USD, unit="USD"),
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        notification_type="ACTUAL",
                        comparison_operator="GREATER_THAN",
                        threshold=pct,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            subscription_type="EMAIL", address=email
                        )
                    ],
                )
                for pct in ALERT_PERCENTAGES
            ],
        )
