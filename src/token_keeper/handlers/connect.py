"""``GET /connect``: verify the signed link, create OAuth state, go to GitHub.

Spec §6.1 and ADR 0007. The state is 32 random bytes and single-use; the PKCE
verifier is stored as KMS ciphertext bound to the state's hash (ADR 0003). Rejections
get one plain error page and a logged reason code, never the request's parameters.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any

from token_keeper import crypto, github, ids, log, metrics, pages, settings
from token_keeper.contract import connect_links
from token_keeper.handlers._shared import crypto as kms
from token_keeper.handlers._shared import now, store
from token_keeper.store import STATE_TTL_SECONDS, StateRecord


def pkce_pair() -> tuple[str, str]:
    """A verifier (64 URL-safe characters) and its S256 challenge."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


@metrics.flushed
def handler(event: dict[str, Any], context: object) -> pages.Response:
    logger = log.start(handler="connect")
    cfg = settings.get()
    current = now()
    try:
        request = connect_links.verify_query(
            cfg.connect_link_secret,
            event.get("rawQueryString") or "",
            allowed_return_to=cfg.allowed_return_to,
            now=current,
        )
    except connect_links.ConnectLinkError as exc:
        logger.info("connect.rejected", extra={"reason": exc.reason})
        metrics.count(metrics.CONNECT_REJECTED)
        return pages.error_page()

    correlation_id = ids.new("cor")
    log.add_keys(correlation_id=correlation_id, app_id=request.app_id)

    state = secrets.token_urlsafe(32)
    hashed = crypto.state_hash(state)
    verifier, challenge = pkce_pair()
    store().put_state(
        hashed,
        StateRecord(
            app_id=request.app_id,
            external_user_id=request.external_user_id,
            return_to=request.return_to,
            correlation_id=correlation_id,
            pkce_verifier_ct=kms().encrypt(verifier, crypto.state_context(hashed)),
            expires_at=current + STATE_TTL_SECONDS,
        ),
    )
    logger.info("connect.redirect")
    return pages.redirect(
        github.authorize_url(cfg.github_client_id, cfg.callback_url, state, challenge)
    )
