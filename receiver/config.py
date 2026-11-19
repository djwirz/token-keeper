"""Receiver configuration.

Locally, secrets come straight from environment variables (the local runner writes
them to ``.env.local``). In AWS, each secret's environment variable names an SSM
SecureString parameter instead (``…_PARAM``), read once per cold start.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cache

import boto3

from token_keeper.contract import webhooks


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


def _secret(name: str) -> str:
    direct = os.environ.get(name)
    if direct:
        return direct
    param = os.environ.get(f"{name}_PARAM")
    if not param:
        raise RuntimeError(f"set {name} or {name}_PARAM")
    ssm = boto3.client("ssm")
    value: str = ssm.get_parameter(Name=param, WithDecryption=True)["Parameter"]["Value"]
    return value


@cache
def load() -> Config:
    """Read configuration once per process. A malformed webhook secret fails here."""
    webhook_secret = _secret("WEBHOOK_SECRET")
    webhooks.check_secret(webhook_secret)
    return Config(
        table_name=os.environ["RECEIVER_TABLE"],
        webhook_secret=webhook_secret,
        connect_link_secret=_secret("CONNECT_LINK_SECRET"),
        passcode=_secret("DEMO_PASSCODE"),
        token_keeper_connect_url=os.environ["TOKEN_KEEPER_CONNECT_URL"],
        public_url=os.environ["RECEIVER_PUBLIC_URL"],
    )
