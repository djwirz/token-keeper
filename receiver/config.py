"""Receiver configuration.

Locally, secrets and Token Keeper's connect URL come straight from environment
variables (the local runner sets them). In AWS, each one's ``…_PARAM`` variable names
an SSM parameter instead, read once per cold start (``token_keeper.ssm``, ADR 0014).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cache

from token_keeper.contract import webhooks
from token_keeper.ssm import env_or_ssm


@dataclass(frozen=True)
class Config:
    table_name: str
    webhook_secret: str
    connect_link_secret: str
    passcode: str
    token_keeper_connect_url: str
    public_url: str
    app_id: str = "demo"
    demo_user: str = "u_demo"


@cache
def load() -> Config:
    """Read configuration once per process. A malformed webhook secret fails here."""
    webhook_secret = env_or_ssm("WEBHOOK_SECRET")
    webhooks.check_secret(webhook_secret)
    return Config(
        table_name=os.environ["RECEIVER_TABLE"],
        webhook_secret=webhook_secret,
        connect_link_secret=env_or_ssm("CONNECT_LINK_SECRET"),
        passcode=env_or_ssm("DEMO_PASSCODE"),
        token_keeper_connect_url=env_or_ssm("TOKEN_KEEPER_CONNECT_URL"),
        public_url=os.environ["RECEIVER_PUBLIC_URL"],
    )
