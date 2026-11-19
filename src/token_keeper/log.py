"""Structured logging (Powertools, ADR 0009) with a token scrubber (spec §8).

Every line goes through ``ScrubbingFormatter``, which replaces anything shaped like a
GitHub token (``ghu_``, ``ghr_`` and the other ``gh?_`` prefixes) after the record is
rendered, so it covers the message, extra keys and exception tracebacks alike. The
same formatter is copied onto every other registered logger (httpx, botocore).

Code never logs tokens on purpose; the scrubber is the backstop, and
``tests/test_logging.py`` checks that nothing token-shaped gets through a whole flow.
"""

from __future__ import annotations

import io
import logging
import re
import sys
from functools import cache
from typing import IO, Any, cast

from aws_lambda_powertools import Logger
from aws_lambda_powertools.logging.formatter import LambdaPowertoolsFormatter
from aws_lambda_powertools.logging.utils import copy_config_to_registered_loggers

SERVICE = "token-keeper"
TOKEN_PATTERN = re.compile(r"gh[pousr]_[A-Za-z0-9_]*")
REDACTED = "[redacted-token]"


def scrub(text: str) -> str:
    return TOKEN_PATTERN.sub(REDACTED, text)


class ScrubbingFormatter(LambdaPowertoolsFormatter):
    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


class _CurrentStdout(io.TextIOBase):
    """Writes to whatever ``sys.stdout`` is now, not what it was at import.

    Lambda never swaps stdout, but test capture does.
    """

    def write(self, text: str) -> int:
        return sys.stdout.write(text)

    def flush(self) -> None:
        sys.stdout.flush()


@cache
def get_logger() -> Logger:
    logger = Logger(
        service=SERVICE,
        logger_formatter=ScrubbingFormatter(),
        stream=cast(IO[str], _CurrentStdout()),
    )
    copy_config_to_registered_loggers(source_logger=logger)
    return logger


def start(**keys: Any) -> Logger:
    """Begin one invocation: drop the previous one's keys, then add these.

    Keys are thread-local, so the local runner's threads don't mix correlation ids.
    """
    logger = get_logger()
    logger.thread_safe_clear_keys()
    if keys:
        logger.thread_safe_append_keys(**keys)
    return logger


def add_keys(**keys: Any) -> None:
    get_logger().thread_safe_append_keys(**keys)
