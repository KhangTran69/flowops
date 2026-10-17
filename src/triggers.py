"""Scheduled trigger processor for interval and daily triggers (v1.4).

Fires due interval/daily triggers, generates a deterministic event_id for
each occurrence, and inserts a trigger_delivery row. Uses the same
lease/fencing pattern as jobs and timers so two schedulers cannot double-fire.

Run once (useful in tests):

    python -m src.triggers --once

Or as a polling daemon:

    python -m src.triggers --poll --interval 2
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from src import db

SCHEDULE_KINDS = ("webhook", "interval", "daily")
TICK_COUNTS = {"fired": 0, "empty": 0, "skipped": 0}
DEFAULT_LEASE_SECONDS = 60


def _now(value: Optional[datetime] = None) -> datetime:
    return value or db.utcnow()


def _parse_daily_at(value: str) -> tuple[int, int, int]:
    """Parse ``HH:MM:SS`` into (hours, minutes, seconds)."""
    parts = value.strip().split(":")
    if len(parts) == 3:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    elif len(parts) == 2:
        h, m, s = int(parts[0]), int(parts[1]), 0
    else:
        raise ValueError(f"Invalid daily_at_utc: {value!r}")
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
        raise ValueError(f"daily_at_utc out of range: {value!r}")
    return h, m, s


def next_daily_window(last_fired: Optional[datetime], daily_at: str, now: datetime) -> datetime:
    """Return the next UTC datetime at which a daily trigger should fire."""
    h, m, s = _parse_daily_at(daily_at)
    today_target = now.replace(hour=h, minute=m, second=s, microsecond=0, tzinfo=timezone.utc)
    if last_fired is not None:
        last_fired = last_fired if last_fired.tzinfo else last_fired.replace(tzinfo=timezone.utc)
    if last_fired is None or last_fired < today_target <= now:
        return today_target
    return today_target + timedelta(days=1)


def is_due(trigger: db.TriggerRecord, now: datetime) -> bool:
    """Check whether a scheduled trigger is due to fire."""
    if trigger.schedule_kind == "interval":
        if not trigger.interval_seconds or trigger.interval_seconds <= 0:
            return False
        last = trigger.last_fired_at
        if last is not None:
            last = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
        if last is not None and (now - last).total_seconds() < trigger.interval_seconds:
            return False
        return True
    if trigger.schedule_kind == "daily":
        if not trigger.daily_at_utc:
            return False
        target = next_daily_window(trigger.last_fired_at, trigger.daily_at_utc, now)
        return target <= now
    return False


def _window_start(trigger: db.TriggerRecord, now: datetime) -> datetime:
    """Deterministic window start for event_id generation."""
    if trigger.schedule_kind == "interval":
        last = trigger.last_fired_at
        if last is not None:
            last = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
        return last or (now - timedelta(seconds=trigger.interval_seconds or 0))
    if trigger.schedule_kind == "daily":
        return next_daily_window(trigger.last_fired_at, trigger.daily_at_utc, now)
    return now


def _event_id(trigger_key: str, window: datetime) -> str:
    return f"schedule:{trigger_key}:{window.strftime('%Y-%m-%dT%H:%M:%S')}"


def claim_and_fire(
    session: Session,
    *,
    now: Optional[datetime] = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> list[db.TriggerDeliveryRecord]:
    """Claim due scheduled triggers and insert deliveries.

    Uses a conditional update on ``last_fired_at`` as the fencing token: only
    one scheduler can advance it, so a second scheduler sees the trigger as
    not-due and skips it.
    """
    at = _now(now)
    table = db.TriggerRecord
    candidates = session.execute(
        select(table).where(
            and_(
                table.active.is_(True),
                table.schedule_kind.in_(("interval", "daily")),
            )
        )
    ).scalars().all()

    deliveries: list[db.TriggerDeliveryRecord] = []
    for trigger in candidates:
        if not is_due(trigger, at):
            continue
        window = _window_start(trigger, at)
        event_id = _event_id(trigger.key, window)

        # Fencing: conditionally set last_fired_at only if it hasn't changed.
        fence_condition = and_(
            table.key == trigger.key,
            or_(
                table.last_fired_at.is_(None),
                table.last_fired_at == trigger.last_fired_at,
            ),
        )
        result = session.execute(
            update(table)
            .execution_options(synchronize_session=False)
            .where(fence_condition)
            .values(
                last_fired_at=at,
                updated_at=at,
            )
        )
        if result.rowcount != 1:
            TICK_COUNTS["skipped"] += 1
            continue

        # Insert delivery (dedup by unique constraint on trigger_key + event_id).
        existing = session.execute(
            select(db.TriggerDeliveryRecord).where(
                and_(
                    db.TriggerDeliveryRecord.trigger_key == trigger.key,
                    db.TriggerDeliveryRecord.event_id == event_id,
                )
            )
        ).scalar_one_or_none()

        if existing is not None:
            TICK_COUNTS["skipped"] += 1
            continue

        delivery = db.TriggerDeliveryRecord(
            trigger_key=trigger.key,
            event_id=event_id,
            payload_json=json.dumps(
                {"event_type": trigger.event_type, "fired_at": at.isoformat()},
                separators=(",", ":"),
                default=str,
            ),
            status="received",
            received_at=at,
        )
        session.add(delivery)
        session.flush()
        deliveries.append(delivery)
        db.log_execution(
            session,
            run_id=uuid.uuid4().hex[:12],
            action="trigger.delivery.received",
            status="success",
            detail=f"scheduled trigger {trigger.key!r} fired (event_id={event_id})",
            actor_subject=f"trigger:{trigger.key}",
        )
        TICK_COUNTS["fired"] += 1

    if deliveries:
        session.commit()
    else:
        session.rollback()
    return deliveries


def tick(*, now: Optional[datetime] = None) -> list[db.TriggerDeliveryRecord]:
    """One scheduler tick: fire all due triggers."""
    db._ensure_tables()
    session = db.SessionLocal()
    try:
        return claim_and_fire(session, now=now)
    finally:
        session.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="FlowOps scheduled trigger processor")
    parser.add_argument("--once", action="store_true", help="fire due triggers once")
    parser.add_argument("--poll", action="store_true", help="poll continuously")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between ticks")
    args = parser.parse_args(argv)
    db._ensure_tables()
    if args.once or not args.poll:
        deliveries = tick()
        print(f"fired {len(deliveries)} trigger delivery(ies)")
        return 0
    while True:
        tick()
        time.sleep(max(0.1, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
