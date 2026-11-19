"""``GET /callback``: finish the OAuth flow (spec §6.2, ADR 0011).

1. Take the state record (deleted on read, so it works once). Missing, unknown or
   expired → error page.
2. GitHub sent an error (the user pressed Cancel) → back to ``return_to?result=denied``.
3. Exchange the code with the PKCE verifier, then ``GET /user``.
4. Write the account (new, or a takeover of the existing one), or stop with
   ``already_linked`` if this GitHub account belongs to another main-app user.
5. Enqueue ``account.connected`` and redirect with ``?result=connected``.

Tokens exist only in memory here and are encrypted with the ``account_id`` context
before anything is written.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from token_keeper import crypto, delivery, github, ids, log, pages, settings
from token_keeper.handlers._shared import crypto as kms
from token_keeper.handlers._shared import now, store
from token_keeper.store import EncryptedTokens, Identity, StateRecord, WriteConflict

Outcome = Literal["connected", "already_linked"]


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def profile_snapshot(user: github.GitHubUser, fetched_at: int) -> dict[str, Any]:
    """The snapshot at connect time: profile only, no events yet (spec §6.3)."""
    return {
        "fetched_at": iso(fetched_at),
        "profile": {
            "name": user.name,
            "avatar_url": user.avatar_url,
            "public_repos": user.public_repos,
            "followers": user.followers,
        },
        "recent_events": [],
    }


def _encrypt(account_id: str, tokens: github.TokenPair) -> EncryptedTokens:
    context = crypto.account_context(account_id)
    return EncryptedTokens(
        access_token_ct=kms().encrypt(tokens.access_token, context),
        refresh_token_ct=kms().encrypt(tokens.refresh_token, context),
        access_expires_at=tokens.access_expires_at,
        refresh_expires_at=tokens.refresh_expires_at,
    )


def save_connection(
    record: StateRecord, tokens: github.TokenPair, user: github.GitHubUser, current: int
) -> tuple[Outcome, str | None]:
    """Decide new, reconnect or already linked, and write. Returns the account id.

    A write that loses a race (two first connects at once, a worker's lease bumping
    the version) re-reads and decides once more before giving up.
    """
    db = store()
    identity = Identity(github_user_id=user.id, login=user.login)
    snapshot = profile_snapshot(user, current)
    for attempt in (1, 2):
        account_id = db.account_for_user(record.app_id, record.external_user_id)
        owner = db.account_for_github_user(user.id)
        if owner is not None and owner != account_id:
            return "already_linked", None
        try:
            if account_id is None:
                account_id = ids.new("acc")
                db.create_account(
                    account_id=account_id,
                    app_id=record.app_id,
                    external_user_id=record.external_user_id,
                    identity=identity,
                    tokens=_encrypt(account_id, tokens),
                    snapshot=snapshot,
                    now=current,
                )
                log.add_keys(account_id=account_id)
                return "connected", account_id
            account = db.get_account(account_id)
            if account is None:
                raise RuntimeError("user link points at a missing account")
            log.add_keys(account_id=account_id)
            db.reconnect_account(
                account,
                identity=identity,
                tokens=_encrypt(account_id, tokens),
                snapshot=snapshot,
                now=current,
            )
            return "connected", account_id
        except WriteConflict:
            log.get_logger().info("callback.write_conflict", extra={"attempt": attempt})
    raise WriteConflict


def _back(return_to: str, result: str) -> pages.Response:
    # return_to is on the allow-list and has no query (spec §6.1).
    return pages.redirect(f"{return_to}?result={result}")


def handler(event: dict[str, Any], context: object) -> pages.Response:
    logger = log.start(handler="callback")
    cfg = settings.get()
    current = now()
    query = event.get("queryStringParameters") or {}

    state = query.get("state")
    if not state:
        logger.info("callback.rejected", extra={"reason": "missing_state"})
        return pages.error_page()
    hashed = crypto.state_hash(state)
    record = store().take_state(hashed, current)
    if record is None:
        logger.info("callback.rejected", extra={"reason": "unknown_state"})
        return pages.error_page()
    log.add_keys(correlation_id=record.correlation_id, app_id=record.app_id)

    if "error" in query:
        logger.info("callback.denied", extra={"github_error": github.safe_code(query["error"])})
        return _back(record.return_to, "denied")
    code = query.get("code")
    if not code:
        logger.info("callback.rejected", extra={"reason": "missing_code"})
        return pages.error_page()

    verifier = kms().decrypt(record.pkce_verifier_ct, crypto.state_context(hashed))
    try:
        tokens = github.exchange_code(
            client_id=cfg.github_client_id,
            client_secret=cfg.github_client_secret,
            code=code,
            redirect_uri=cfg.callback_url,
            code_verifier=verifier,
            now=current,
        )
        user = github.get_user(tokens.access_token)
    except github.GitHubError as exc:
        logger.warning("callback.github_failed", extra={"reason": exc.reason})
        return pages.error_page(502)

    try:
        outcome, account_id = save_connection(record, tokens, user, current)
    except WriteConflict:
        logger.warning("callback.rejected", extra={"reason": "write_conflict"})
        return pages.error_page(409)
    if outcome == "already_linked" or account_id is None:
        logger.info("callback.already_linked", extra={"github_user_id": user.id})
        return _back(record.return_to, "already_linked")

    webhook_id = delivery.enqueue(
        cfg.delivery_queue_url,
        account_id=account_id,
        event_type="account.connected",
        correlation_id=record.correlation_id,
    )
    logger.info("callback.connected", extra={"webhook_id": webhook_id, "login": user.login})
    return _back(record.return_to, "connected")
