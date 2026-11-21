"""The alarm topic and its email subscription (spec §8, ADR 0019).

Like ``Budget``, it's deployed by hand and never by CI, so the address stays out of the
repo and GitHub: it exists in the app only when ``ALERT_EMAIL`` is set. It has no
assets, so it uses the legacy synthesizer and deploys with the operator's own
credentials (ADR 0016).

TokenKeeper's alarms (deployed by CI) find the topic by its fixed name, so neither
stack exports anything. Until this stack exists the alarms still change state, but
their notifications go nowhere.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import LegacyStackSynthesizer, Stack
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from constructs import Construct

from infra.stacks.shared import ALARM_TOPIC_NAME


class AlertsStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, email: str, **kwargs: Any) -> None:
        kwargs.setdefault("synthesizer", LegacyStackSynthesizer())
        super().__init__(scope, construct_id, **kwargs)
        topic = sns.Topic(
            self,
            "AlarmTopic",
            topic_name=ALARM_TOPIC_NAME,
            display_name="token-keeper alarms",
        )
        # AWS emails a confirmation link; nothing arrives until Dan clicks it.
        topic.add_subscription(subscriptions.EmailSubscription(email))
