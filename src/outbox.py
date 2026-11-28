"""Leased outbox dispatcher helpers.

The dispatcher claims a pending delivery with a unique owner token and lease,
delivers once, then commits a conditional transition. A stale owner cannot
overwrite a takeover or double-finalize the row.
"""

import json
import os
import uuid
from typing import Any
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, true, update
from sqlalchemy.orm import Session

from src import db, jobs

DEFAULT_LEASE_SECONDS = 60


def _due(now: datetime, *, manual: bool = False):
    table = db.NotificationOutboxRecord
    return and_(
        table.status == "pending",
        true() if manual else or_(table.next_attempt_at.is_(None), table.next_attempt_at <= now),
        or_(
            table.lease_owner_token.is_(None),
            table.lease_expires_at.is_(None),
            table.lease_expires_at <= now,
        ),
    )


def claim_delivery(
    session: Session,
    worker_id: str | None = None,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
    notification_id: int | None = None,
    manual: bool = False,
    actor_subject: str = "notification-worker",
) -> db.NotificationOutboxRecord | None:
    """Claim one due pending delivery for this dispatcher."""
    if lease_seconds <= 0:
        raise ValueError("Notification lease must be positive.")
    now = now or db.utcnow()
    token = uuid.uuid4().hex
    table = db.NotificationOutboxRecord
    condition = _due(now, manual=manual)
    query = select(table.id).where(condition).order_by(table.next_attempt_at, table.id).limit(10)
    if notification_id is not None:
        query = query.where(table.id == notification_id)
    if session.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    candidates = session.execute(query).scalars().all()
    for notification_id in candidates:
        # A claim consumes one attempt before any external request. A crash
        # after sending is ambiguous; stop after the budget, rather than
        # silently delivering forever. Explicit requeue clears the lease
        # and permits one further attempt, preserving history.
        exhausted = session.execute(
            update(table).execution_options(synchronize_session=False)
            .where(table.id == notification_id, condition,
                   table.lease_owner_token.is_not(None),
                   table.attempts >= max_attempts_from_environment())
            .values(status="failed", lease_owner_token=None, lease_expires_at=None,
                    next_attempt_at=None, updated_at=now,
                    last_error="Abandoned delivery exhausted retry budget; reconcile external effects before retry.")
        )
        if exhausted.rowcount == 1:  # type: ignore[attr-defined]
            record = session.get(table, notification_id, populate_existing=True)
            db.log_execution(session, run_id=uuid.uuid4().hex[:12], action="notification.dead_lettered",
                             status="failure", ticket_id=record.ticket_id if record else None,
                             duration_ms=0, detail=f"outbox #{notification_id}: abandoned attempt budget exhausted",
                             actor_subject=actor_subject)
            continue
        result = session.execute(
            update(table)
            .execution_options(synchronize_session=False)
            .where(and_(table.id == notification_id, condition))
            .values(
                lease_owner_token=token,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                attempts=table.attempts + 1,
                updated_at=now,
            )
        )
        if result.rowcount == 1:  # type: ignore[attr-defined]  # rowcount is CursorResult-only at runtime
            session.commit()
            session.expire_all()
            return session.get(table, notification_id)
    session.commit()
    return None


def finalize_delivery(
    session: Session,
    notification_id: int,
    owner_token: str,
    *,
    success: bool,
    attempt_delta: int,
    error: str | None,
    max_attempts: int,
    now: datetime | None = None,
    delay_seconds: int | None = None,
) -> str | None:
    """Finalize a claimed delivery, or return None if the lease was lost."""
    now = now or db.utcnow()
    table = db.NotificationOutboxRecord
    record = session.get(table, notification_id)
    if (
        record is None
        or record.status != "pending"
        or record.lease_owner_token != owner_token
    ):
        return None
    # The claim already recorded the first attempt durably.
    attempts = record.attempts + max(0, attempt_delta - 1)
    values = {
        "attempts": attempts,
        "last_error": error,
        "lease_owner_token": None,
        "lease_expires_at": None,
        "updated_at": now,
    }
    if success:
        values.update(status="delivered", next_attempt_at=None)
        resulting = "delivered"
    elif attempts >= max_attempts:
        values.update(status="failed", next_attempt_at=None)
        resulting = "failed"
    else:
        delay = jobs.backoff_seconds(attempts) if delay_seconds is None else max(0, delay_seconds)
        values.update(
            status="pending",
            next_attempt_at=now + timedelta(seconds=delay),
        )
        resulting = "pending"
    outcome = session.execute(
        update(table)
        .execution_options(synchronize_session=False)
        .where(
            and_(
                table.id == notification_id,
                table.status == "pending",
                table.lease_owner_token == owner_token,
                table.lease_expires_at > now,
            )
        )
        .values(**values)
    )
    return resulting if outcome.rowcount == 1 else None  # type: ignore[attr-defined]  # rowcount is CursorResult-only at runtime


def decode_payload(value: str) -> dict[str, Any] | None:
    """Invalid stored envelopes must be dead-lettered, not crash the daemon."""
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def max_attempts_from_environment() -> int:
    """Read the same bounded dead-letter threshold as the API."""
    try:
        configured = int(os.environ.get("FLOWOPS_NOTIFICATION_MAX_ATTEMPTS", "5"))
        return max(1, min(20, configured))
    except (TypeError, ValueError):
        return 5
