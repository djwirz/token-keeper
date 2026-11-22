"""Settings from the environment locally, or from SSM Parameter Store in AWS.

Locally, each value is an environment variable (the local runner loads ``.env.local``).
In AWS the variable is absent and ``<NAME>_PARAM`` names a parameter instead: a
SecureString for secrets, a plain String for the URLs the two stacks publish for each
other (ADR 0014). Read once per cold start by whoever caches the config.
"""

from __future__ import annotations

import os

import boto3


def env_or_ssm(name: str) -> str:
    value = optional_env_or_ssm(name)
    if value is None:
        raise RuntimeError(f"set {name} or {name}_PARAM")
    return value


def optional_env_or_ssm(name: str) -> str | None:
    """Like ``env_or_ssm``, but ``None`` when neither variable is set."""
    direct = os.environ.get(name)
    if direct:
        return direct
    param = os.environ.get(f"{name}_PARAM")
    if not param:
        return None
    ssm = boto3.client("ssm")
    value: str = ssm.get_parameter(Name=param, WithDecryption=True)["Parameter"]["Value"]
    return value
