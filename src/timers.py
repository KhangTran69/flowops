"""SLA timers for checklist steps (v1.3).

Lifecycle of one timer row (``workflow_timers``):

* **created** when a step enters ``pending`` — either at checklist creation
  or when a blocked step is promoted to pending after its prerequisites
  complete ("the clock starts at promotion time").
* **cancelled** as soon as the step leaves ``pending`` (completed/failed/
  skipped/approval_required/in_progress).
* **fired** by this module's poller when ``due_at`` passes while the step is
  still pending.  Firing uses a conditional UPDATE on ``status='pending'``
  plus a unique lease token (the same fencing pattern as src/jobs.py), so two
  concurrent timer processes can never double-fire the same deadline.

When a timer fires we:

1. write an ``onboarding.step.overdue`` execution log (actor
   ``scheduler:<pid>``) including elapsed minutes,
2. enqueue a notification through the existing retryable outbox, and
3. if the step defines an ``escalation_queue``, reassign the step's queue.

Run it manually::

    python -m src.timers --once            # fire everything due, then exit
    python -m src.timers --poll --interval 2

Tests inject their own clock via the ``now`` parameter — no sleeping.
"""

from __future__ import annotations

import argparse
import os
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy import and_, select, update

from src import db

DEFAULT_LEASE_SECONDS = 60
TIMER_ACTOR_PREFIX = "scheduler"


def timer_actor() -> str:
    """Log actor identity for the timer process (pid makes it traceable)."""
    return f"{TIMER_ACTOR_PREFIX}:{os.getpid()}"


def _aware(value: datetime) -> datetime:
    """Treat naive datetimes as UTC so SQLite and PostgreSQL behave alike."""
    from datetime import timezone

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def ensure_step_timer(
    session,
    item,
    *,
    now: Optional[datetime] = None,
) -> Optional[db.WorkflowTimerRecord]:
    """Create (or refresh) the SLA timer for a step that just became pending.

    Steps without ``sla_minutes`` get no timer.  An existing pending timer is
    re-armed to the new deadline; fired/cancelled rows are left alone and a
    fresh row is created instead (history stays intact).
    """
    if not getattr(item, "sla_minutes", None) or item.status != "pending":
        return None
    now = now or db.utcnow()
    due_at = now + timedelta(minutes=int(item.sla_minutes))
    existing = session.execute(
        select(db.WorkflowTimerRecord)
        .where(
            and_(
                db.WorkflowTimerRecord.item_id == item.id,
                db.WorkflowTimerRecord.kind == "step_sla",
                db.WorkflowTimerRecord.status == "pending",
            )
        )
    ).scalars().first()
    if existing is not None:
        existing.due_at = due_at
        session.flush()
        return existing
    timer = db.WorkflowTimerRecord(
        request_id=item.request_id,
        item_id=item.id,
        kind="step_sla",
        due_at=due_at,
        status="pending",
    )
    session.add(timer)
    session.flush()
    return timer


def cancel_step_timer(session, item_id: int) -> int:
    """Cancel any pending timer for a step that left 'pending'. Returns count."""
    timers = session.execute(
        select(db.WorkflowTimerRecord).where(
            and_(
                db.WorkflowTimerRecord.item_id == item_id,
                db.WorkflowTimerRecord.status == "pending",
            )
        )
    ).scalars().all()
    for timer in timers:
        timer.status = "cancelled"
    session.flush()
    return len(timers)


def claim_due_timer(
    session,
    *,
    now: Optional[datetime] = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> Optional[db.WorkflowTimerRecord]:
    """Atomically claim the oldest overdue pending timer (jobs.py pattern)."""
    now = now or db.utcnow()
    table = db.WorkflowTimerRecord
    token = uuid.uuid4().hex
    candidate_query = (
        select(table.id)
        .where(and_(table.status == "pending", table.due_at <= now))
        .order_by(table.due_at, table.id)
        .limit(10)
    )
    if session.get_bind().dialect.name == "postgresql":
        candidate_query = candidate_query.with_for_update(skip_locked=True)
    candidate_ids = session.execute(candidate_query).scalars().all()
    for timer_id in candidate_ids:
        result = session.execute(
            update(table).execution_options(synchronize_session=False)
            .where(
                and_(
                    table.id == timer_id,
                    table.status == "pending",          # conditional fire
                    table.due_at <= now,
                    and_(
                        table.lease_owner_token.is_(None),
                        table.lease_expires_at.is_(None),
                    )
                    | (table.lease_expires_at < now),   # stale lease takeover
                )
            )
            .values(
                lease_owner_token=token,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                attempts=table.attempts + 1,
            )
        )
        if result.rowcount == 1:
            session.commit()
            session.expire_all()
            return session.get(table, timer_id)
    session.commit()
    return None


def fire_timer(session, timer: db.WorkflowTimerRecord, *, now: Optional[datetime] = None) -> bool:
    """Process one claimed timer; returns True when it was marked fired.

    If the step is no longer pending (someone completed it between due time
    and pickup) the timer is cancelled instead — that is not an error.
    """
    now = now or db.utcnow()
    item = session.get(db.ChecklistItemRecord, timer.item_id)
    record = session.get(db.OnboardingRequestRecord, timer.request_id)
    if (
        item is None
        or record is None
        or item.status != "pending"
        or record.status in ("completed", "cancelled")
    ):
        # The step left pending before we got here: cancel, do not alert.
        return _finalize(session, timer, "cancelled", now)

    elapsed = max(0, int((_aware(now) - _aware(timer.due_at)).total_seconds() // 60))
    # Commit the alert side effects (log + optional escalation + outbox row)
    # in one transaction BEFORE releasing the lease.  A crash between this
    # commit and _finalize leaves the timer pending with an expired lease, so
    # another process takes over and re-alerts: at-least-once, never lost.
    db.log_execution(
        session,
        run_id=uuid.uuid4().hex[:12],
        action="onboarding.step.overdue",
        status="warning",
        duration_ms=0,
        detail=(
            f"step '{item.step}' (#{item.id}) of request #{record.id} overdue by "
            f"{elapsed} minute(s); sla={item.sla_minutes}m"
        ),
        actor_subject=timer_actor(),
    )
    if getattr(item, "escalation_queue", None):
        previous_queue = item.queue
        item.queue = item.escalation_queue
        db.log_execution(
            session,
            run_id=uuid.uuid4().hex[:12],
            action="onboarding.step.escalated",
            status="success",
            duration_ms=0,
            detail=(
                f"step '{item.step}' reassigned {previous_queue} -> "
                f"{item.escalation_queue} after SLA breach"
            ),
            actor_subject=timer_actor(),
        )
    _enqueue_overdue_notification(session, record, item, elapsed)
    session.commit()
    return _finalize(session, timer, "fired", now)


def _finalize(session, timer: db.WorkflowTimerRecord, status: str, now: datetime) -> bool:
    """Lease-fenced final transition: only the current lease owner may finish.

    Mirrors src/jobs.py complete/fail: a conditional UPDATE on
    ``lease_owner_token`` means a process whose lease was taken over can never
    mark the timer fired/cancelled — it returns False instead.  The log and
    outbox rows written before this call are already committed with the
    claim; if fencing fails they are simply not repeated (at-least-once with
    visible audit, never double-fired by live owners).
    """
    table = db.WorkflowTimerRecord
    result = session.execute(
        update(table).execution_options(synchronize_session=False)
        .where(
            and_(
                table.id == timer.id,
                table.status == "pending",
                table.lease_owner_token == timer.lease_owner_token,
            )
        )
        .values(
            status=status,
            fired_at=now if status == "fired" else None,
            lease_owner_token=None,
            lease_expires_at=None,
        )
    )
    session.commit()
    session.expire_all()
    return result.rowcount == 1


def _enqueue_overdue_notification(session, record, item, elapsed_minutes: int) -> None:
    """Put the overdue alert into the existing retryable notification outbox."""
    import json

    from src.api import _notification_max_attempts  # late import avoids cycle

    payload = {
        "event": "onboarding.step.overdue",
        "request_id": record.id,
        "request_type": record.request_type,
        "employee_name": record.employee_name,
        "step": item.step,
        "step_key": item.step_key,
        "queue": item.queue,
        "sla_minutes": item.sla_minutes,
        "overdue_minutes": elapsed_minutes,
    }
    session.add(
        db.NotificationOutboxRecord(
            url=os.environ.get("FLOWOPS_WEBHOOK_URL", "http://localhost:9999/missing"),
            payload_json=json.dumps(payload, sort_keys=True),
            ticket_id=None,
            status="pending",
            attempts=0,
            last_error=None,
            max_attempts=_notification_max_attempts(),
        )
    )
    session.flush()


def run_once(*, now: Optional[datetime] = None) -> int:
    """Fire every currently-due timer exactly once; returns the count fired."""
    fired = 0
    while True:
        session = db.SessionLocal()
        try:
            timer = claim_due_timer(session, now=now)
            if timer is None:
                return fired
            if fire_timer(session, timer, now=now):
                fired += 1
        finally:
            session.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="FlowOps SLA timer poller")
    parser.add_argument("--once", action="store_true", help="fire due timers, then exit")
    parser.add_argument("--poll", action="store_true", help="run forever")
    parser.add_argument("--interval", type=float, default=5.0, help="poll interval seconds")
    args = parser.parse_args(argv)
    if not (args.once or args.poll):
        parser.error("choose --once or --poll")
    import time as _time

    total = 0
    while True:
        total += run_once()
        if args.once:
            print(f"fired {total} timer(s)")
            return 0
        _time.sleep(args.interval)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
