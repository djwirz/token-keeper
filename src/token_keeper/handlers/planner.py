"""Planner: every 15 minutes from EventBridge Scheduler (spec §4, ADR 0004).

Finds active accounts whose ``next_due_at`` has passed (the ``due`` index), pushes each
one's ``next_due_at`` about 30 minutes ahead, then sends one sync message per account.
The push comes first, so a backed-up queue doesn't collect duplicates. If the send
fails, the push brings the account back on a later run.
"""

from __future__ import annotations

from typing import Any

from token_keeper import ids, log, settings, sync
from token_keeper.handlers._shared import now, store


def handler(event: dict[str, Any], context: object) -> dict[str, int]:
    correlation_id = ids.new("cor")
    logger = log.start(handler="planner", correlation_id=correlation_id)
    current = now()
    db = store()
    due = db.due_accounts(current)
    pushed = [
        account_id
        for account_id in due
        if db.push_due(account_id, current, current + sync.PLANNER_PUSH_SECONDS)
    ]
    if pushed:
        sync.enqueue(settings.get().sync_queue_url, pushed, correlation_id=correlation_id)
    # Stands in for the AccountsDue metric until slice 6.
    logger.info("planner.enqueued", extra={"due": len(due), "enqueued": len(pushed)})
    return {"enqueued": len(pushed)}
