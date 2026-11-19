"""Secrets from the environment locally, or from SSM Parameter Store in AWS.

Locally, each secret is an environment variable (the local runner loads ``.env.local``).
In AWS the variable is absent and ``<NAME>_PARAM`` names a SecureString parameter
instead, read once per cold start by whoever caches the config.
"""

from __future__ import annotations

import os

import boto3


def secret(name: str) -> str:
    direct = os.environ.get(name)
    if direct:
        return direct
    param = os.environ.get(f"{name}_PARAM")
    if not param:
        raise RuntimeError(f"set {name} or {name}_PARAM")
    ssm = boto3.client("ssm")
    value: str = ssm.get_parameter(Name=param, WithDecryption=True)["Parameter"]["Value"]
    return value
