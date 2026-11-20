"""Worker: SQS ``sync-queue`` → lease → refresh? → sync GitHub → write (spec §4, §7).

The lease rules (spec §4, ADR 0004, ADR 0015):
- Tokens come only from the lease write's response, never from an earlier read.
- A rotated pair is written (still under the lease) before it's used (spec §5).
- Every write after the lease is conditional on ``lease_owner = me``. If a reconnect
  or revocation took over, the write fails with ``LeaseLost`` and the worker discards
  everything it holds, a freshly rotated pair included.
- The Lambda timeout (40 s) is below the lease (60 s), and no token call starts with
  less than ``MIN_LEASE_FOR_TOKEN_CALL`` seconds of lease left, so a worker can't
  refresh after its lease has passed to someone else.

Outcomes (spec §7, ADR 0011): success writes the snapshot and sends ``account.updated``.
A state change (``refresh_expired``, ``refresh_rejected``, ``unauthorized``) moves to
``needs_reauth``, deletes the tokens and sends ``account.updated``. A transient error
stays ``active``, backs off and sends nothing.

Only an unexpected exception fails the message, so SQS retries it and, after a few
tries, moves it to the sync DLQ. GitHub trouble is recorded in the account instead.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from token_keeper import crypto, delivery, github, ids, log, settings, snapshot, sync
from token_keeper.delivery import CORRELATION_ATTRIBUTE
from token_keeper.handlers._shared import crypto as kms
from token_keeper.handlers._shared import encrypt_tokens, now, store
from token_keeper.store import LeasedAccount, LeaseLost

MIN_LEASE_FOR_TOKEN_CALL = 20
# An unexpected error is retried a couple of times, then the message goes to the DLQ.
MAX_RECEIVES = 3


class _Sync:
    """One sync of one leased account."""

    def __init__(self, leased: LeasedAccount, correlation_id: str) -> None:
        self.leased = leased
        self.correlation_id = correlation_id
        self.context = crypto.account_context(leased.account.account_id)
        self.refreshed = False
        self.logger = log.get_logger()

    def run(self) -> None:
        current = now()
        tokens = self.leased.tokens
        if tokens.refresh_expires_at <= current:
            # No GitHub call: the refresh token is past its stored expiry (spec §7).
            self.needs_reauth("refresh_expired")
            return
        try:
            access = kms().decrypt(tokens.access_token_ct, self.context)
            if tokens.access_expires_at - current < sync.REFRESH_WINDOW_SECONDS:
                access = self.refresh()
            try:
                user, events = self.fetch(access)
            except github.Unauthorized:
                if self.refreshed:
                    raise
                # A 401 on a token we haven't refreshed: refresh once and retry (spec §7).
                access = self.refresh()
                user, events = self.fetch(access)
        except github.RefreshRejected:
            self.needs_reauth("refresh_rejected")
        except github.Unauthorized:
            self.needs_reauth("unauthorized")
        except github.RateLimited as exc:
            self.transient(exc.reason, sync.rate_limited_until(now(), exc.reset_at))
        except github.GitHubError as exc:
            failures = self.leased.consecutive_failures + 1
            self.transient(exc.reason, sync.backoff_until(now(), failures))
        else:
            self.succeed(user, events)

    def refresh(self) -> str:
        """Rotate the pair, store it under the lease, and return the new access token."""
        current = now()
        if self.leased.lease_until - current < MIN_LEASE_FOR_TOKEN_CALL:
            raise github.GitHubError("lease_too_short")
        cfg = settings.get()
        refresh_token = kms().decrypt(self.leased.tokens.refresh_token_ct, self.context)
        try:
            pair = github.refresh(
                client_id=cfg.github_client_id,
                client_secret=cfg.github_client_secret,
                refresh_token=refresh_token,
                now=current,
            )
        except github.GitHubError as exc:
            self.logger.warning("refresh.failed", extra={"reason": exc.reason})
            raise
        self.refreshed = True
        # GitHub has already killed the old pair: store the new one before anything else.
        # If this write fails, the pair is lost (spec §7 "Lost rotation").
        encrypted = encrypt_tokens(self.leased.account.account_id, pair)
        store().store_refreshed_tokens(self.leased, encrypted)
        # From here on, the only tokens this sync holds are the ones just stored: the old
        # refresh token is dead at GitHub (PR 4 review: don't leave it in reach).
        self.leased = dataclasses.replace(self.leased, tokens=encrypted)
        self.logger.info("refresh.succeeded")
        return pair.access_token

    def fetch(self, access: str) -> tuple[github.GitHubUser, list[dict[str, str]]]:
        current = now()
        user = github.get_user(access, current)
        # The login from this /user call, not the stored one (spec §5).
        events = github.get_public_events(user.login, access, current)
        return user, events

    def succeed(self, user: github.GitHubUser, events: list[dict[str, str]]) -> None:
        current = now()
        store().finish_sync(
            self.leased,
            login=user.login,
            snapshot=snapshot.build(user, current, events),
            next_due_at=sync.next_sync_at(current),
        )
        self.send_event()
        self.logger.info(
            "sync.succeeded", extra={"refreshed": self.refreshed, "events": len(events)}
        )

    def needs_reauth(self, reason: str) -> None:
        store().finish_needs_reauth(self.leased, reason=reason)
        self.send_event()
        self.logger.warning("sync.needs_reauth", extra={"reason": reason})

    def transient(self, reason: str, next_due_at: int) -> None:
        store().finish_transient(self.leased, error_code=reason, next_due_at=next_due_at)
        self.logger.warning(
            "sync.failed",
            extra={"reason": reason, "retry_in": next_due_at - now()},
        )

    def send_event(self) -> None:
        webhook_id = delivery.enqueue(
            settings.get().delivery_queue_url,
            account_id=self.leased.account.account_id,
            event_type="account.updated",
            correlation_id=self.correlation_id,
        )
        log.add_keys(webhook_id=webhook_id)


def sync_account(account_id: str, correlation_id: str) -> None:
    logger = log.start(handler="worker", account_id=account_id, correlation_id=correlation_id)
    leased = store().take_lease(account_id, ids.new("wrk"), now())
    if leased is None:
        # Another worker holds the lease, or the account isn't active. Drop the message:
        # next_due_at brings an active account back on a later planner run (spec §7).
        logger.info("sync.skipped")
        return
    log.add_keys(app_id=leased.account.app_id)
    try:
        _Sync(leased, correlation_id).run()
    except LeaseLost:
        # A reconnect or revocation took over mid-sync; its write holds the right state.
        logger.warning("lease_lost")


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        try:
            message = json.loads(record["body"])
            attributes = record.get("messageAttributes") or {}
            correlation = (attributes.get(CORRELATION_ATTRIBUTE) or {}).get(
                "stringValue"
            ) or ids.new("cor")
            sync_account(message["account_id"], correlation)
        except Exception:
            log.get_logger().exception("sync.error")
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
