"""Leased SLA timers for workflow steps (v1.3)."""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from src import db

PENDING = "pending"
FIRED = "fired"
CANCELLED = "cancelled"


def sync_timers(
    session: Session, request_id: int, *, now: Optional[datetime] = None
) -> None:
    """Create timers for active SLA steps and cancel timers for inactive ones."""
    at = now or db.utcnow()
    items = session.execute(
        select(db.ChecklistItemRecord).where(
            db.ChecklistItemRecord.request_id == request_id
        )
    ).scalars().all()
    timers = session.execute(
        select(db.WorkflowTimerRecord).where(
            db.WorkflowTimerRecord.request_id == request_id,
            db.WorkflowTimerRecord.status == PENDING,
        )
    ).scalars().all()
    by_item = {timer.item_id: timer for timer in timers}
    for item in items:
        active = item.status in ("pending", "approved") and bool(item.sla_minutes)
        existing = by_item.get(item.id)
        if active and existing is None:
            session.add(
                db.WorkflowTimerRecord(
                    request_id=request_id,
                    item_id=item.id,
                    kind="step_sla",
                    due_at=at + timedelta(minutes=int(item.sla_minutes)),
                    status=PENDING,
                )
            )
        elif not active and existing is not None:
            existing.status = CANCELLED


def fire_due(
    session: Session,
    *,
    now: Optional[datetime] = None,
    limit: int = 50,
    lease_seconds: int = 60,
) -> list[db.WorkflowTimerRecord]:
    """Fire due timers once, conditionally, and enqueue escalation notices."""
    at = now or db.utcnow()
    table = db.WorkflowTimerRecord
    candidates = session.execute(
        select(table.id)
        .where(and_(table.status == PENDING, table.due_at <= at))
        .order_by(table.due_at)
        .limit(limit)
    ).scalars().all()
    fired: list[db.WorkflowTimerRecord] = []
    for timer_id in candidates:
        token = uuid.uuid4().hex
        result = session.execute(
            update(table)
            .execution_options(synchronize_session=False)
            .where(and_(table.id == timer_id, table.status == PENDING))
            .values(
                status=FIRED,
                fired_at=at,
                lease_owner_token=token,
                lease_expires_at=at + timedelta(seconds=lease_seconds),
                attempts=table.attempts + 1,
            )
        )
        if result.rowcount != 1:
            continue
        session.flush()
        session.expire_all()
        timer = session.get(table, timer_id)
        if timer is None:
            continue
        item = session.get(db.ChecklistItemRecord, timer.item_id)
        if item is None:
            continue
        elapsed = max(0, int((at - timer.due_at).total_seconds() // 60))
        db.log_execution(
            session,
            run_id=uuid.uuid4().hex[:12],
            action="onboarding.step.overdue",
            status="failure",
            ticket_id=str(timer.request_id),
            duration_ms=elapsed * 60_000,
            detail=f"step '{item.step}' overdue by {elapsed} minute(s)",
            actor_subject=f"scheduler:{os.getpid()}",
        )
        url = os.environ.get("FLOWOPS_ESCALATION_WEBHOOK_URL") or os.environ.get(
            "FLOWOPS_NOTIFICATION_WEBHOOK_URL"
        )
        if url:
            session.add(
                db.NotificationOutboxRecord(
                    url=url,
                    payload_json=json.dumps(
                        {
                            "event": "onboarding.step.overdue",
                            "request_id": timer.request_id,
                            "item_id": item.id,
                            "step": item.step,
                            "minutes": elapsed,
                        }
                    ),
                    ticket_id=str(timer.request_id),
                    status="pending",
                    attempts=0,
                )
            )
        if item.status == "pending":
            escalation = None
            raw = getattr(item, "when_json", None)
            # The definition keeps escalation_queue in the step payload; the
            # checklist column is optional and may be absent on legacy rows.
            escalation = getattr(item, "escalation_queue", None)
            if escalation:
                item.queue = escalation
        fired.append(timer)
    return fired


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll", action="store_true")
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args(argv)
    db._ensure_tables()
    session = db.SessionLocal()
    try:
        while True:
            fired = fire_due(session)
            session.commit()
            if args.once or not args.poll:
                print(f"fired {len(fired)} timer(s)")
                return 0
            time.sleep(max(0.1, args.interval))
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
