"""Token Keeper configuration, read lazily so each Lambda touches only what it uses.

The connect Lambda never reads the GitHub client secret and the deliverer never reads
the KMS key, so their IAM roles don't need them (spec §9). Secrets come from the
environment locally or SSM in AWS (``token_keeper.ssm``).

Environment:
- ``TOKEN_KEEPER_TABLE``, ``KMS_KEY_ID``, ``DELIVERY_QUEUE_URL``
- ``TOKEN_KEEPER_PUBLIC_URL``: this service's base URL; GitHub returns to ``<it>/callback``
- ``GITHUB_CLIENT_ID`` (not a secret)
- ``ALLOWED_RETURN_TO``: JSON ``{"<app_id>": ["<exact return_to URL>", …]}``
- ``RECEIVER_WEBHOOK_URL``: the main app's webhook endpoint (one app in v1)
- secrets: ``GITHUB_CLIENT_SECRET``, ``CONNECT_LINK_SECRET``, ``WEBHOOK_SECRET``
"""

from __future__ import annotations

import json
import os
from functools import cache, cached_property

from token_keeper.contract import webhooks
from token_keeper.ssm import secret


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
        return os.environ["GITHUB_CLIENT_ID"]

    @cached_property
    def github_client_secret(self) -> str:
        return secret("GITHUB_CLIENT_SECRET")

    @cached_property
    def connect_link_secret(self) -> str:
        return secret("CONNECT_LINK_SECRET")

    @cached_property
    def webhook_secret(self) -> str:
        value = secret("WEBHOOK_SECRET")
        webhooks.check_secret(value)
        return value

    @cached_property
    def allowed_return_to(self) -> dict[str, frozenset[str]]:
        raw = json.loads(os.environ["ALLOWED_RETURN_TO"])
        return {app: frozenset(urls) for app, urls in raw.items()}

    @cached_property
    def receiver_webhook_url(self) -> str:
        return os.environ["RECEIVER_WEBHOOK_URL"]


@cache
def get() -> Settings:
    return Settings()
