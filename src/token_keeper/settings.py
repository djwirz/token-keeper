"""Token Keeper configuration, read lazily so each Lambda touches only what it uses.

The connect Lambda never reads the GitHub client secret and the deliverer never reads
the KMS key, so their IAM roles don't need them (spec §9). Secrets come from the
environment locally or SSM in AWS (``token_keeper.ssm``).

Environment:
- ``TOKEN_KEEPER_TABLE``, ``KMS_KEY_ID``, ``DELIVERY_QUEUE_URL``
- ``TOKEN_KEEPER_PUBLIC_URL``: this service's base URL; GitHub returns to ``<it>/callback``
- From the environment or SSM (``…_PARAM``, ADR 0014):
  - ``GITHUB_CLIENT_ID`` (not a secret)
  - ``RECEIVER_URL``: the one registered main app's base URL (spec §2: one app, from
    config). It is the only allowed ``return_to``, and its webhook endpoint is
    ``<it>webhooks/token-keeper``.
  - secrets: ``GITHUB_CLIENT_SECRET``, ``CONNECT_LINK_SECRET``, ``WEBHOOK_SECRET``
"""

from __future__ import annotations

import os
from functools import cache, cached_property

from token_keeper.contract import webhooks
from token_keeper.ssm import env_or_ssm

APP_ID = "demo"
WEBHOOK_PATH = "webhooks/token-keeper"


class Settings:
    @cached_property
    def table_name(self) -> str:
        return os.environ["TOKEN_KEEPER_TABLE"]

    @cached_property
    def kms_key_id(self) -> str:
        return os.environ["KMS_KEY_ID"]

    @cached_property
    def delivery_queue_url(self) -> str:
        return os.environ["DELIVERY_QUEUE_URL"]

    @cached_property
    def callback_url(self) -> str:
        return os.environ["TOKEN_KEEPER_PUBLIC_URL"].rstrip("/") + "/callback"

    @cached_property
    def github_client_id(self) -> str:
        return env_or_ssm("GITHUB_CLIENT_ID")

    @cached_property
    def github_client_secret(self) -> str:
        return env_or_ssm("GITHUB_CLIENT_SECRET")

    @cached_property
    def connect_link_secret(self) -> str:
        return env_or_ssm("CONNECT_LINK_SECRET")

    @cached_property
    def webhook_secret(self) -> str:
        value = env_or_ssm("WEBHOOK_SECRET")
        webhooks.check_secret(value)
        return value

    @cached_property
    def receiver_url(self) -> str:
        return env_or_ssm("RECEIVER_URL")

    @cached_property
    def allowed_return_to(self) -> dict[str, frozenset[str]]:
        return {APP_ID: frozenset({self.receiver_url})}

    @cached_property
    def receiver_webhook_url(self) -> str:
        return self.receiver_url + WEBHOOK_PATH


@cache
def get() -> Settings:
    return Settings()
