"""Event-driven trigger framework (v1.4).

This module provides:
* Safe event-to-request mapping without eval/exec
* Scheduler for interval and daily triggers
* Lease-based fencing to prevent double-firing
* Deterministic event IDs for deduplication
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from src import db

VALID_TARGET_TYPES = ("onboarding_request", "offboarding_request", "ticket")
VALID_SCHEDULE_KINDS = ("webhook", "interval", "daily")
DELIVERY_STATUSES = ("received", "queued", "succeeded", "failed", "skipped")

_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*\Z")


class MappingError(ValueError):
    """Raised when a mapping references an unknown path or fails validation."""


class TriggerError(ValueError):
    """Raised when a trigger cannot be processed or scheduled."""


def _resolve_path(path: str, scope: Mapping[str, Any]) -> Any:
    """Resolve a dotted path like event.data.user against a scope dictionary.

    Unknown paths return None (fail closed). Only dict lookups are allowed.
    """
    if not path:
        return None
    segments = path.split(".")
    current: Any = scope
    for segment in segments:
        if not isinstance(current, Mapping):
            return None
        current = current.get(segment)
        if current is None:
            return None
    return current


def _validate_mapping(mapping: dict[str, Any]) -> None:
    """Validate that a mapping only uses allowed patterns.

    Allowed patterns:
    * Fixed literals (strings, numbers, booleans, null)
    * $event.<dot.path>
    * $trigger.<field>

    Any other pattern raises MappingError.
    """
    for key, value in mapping.items():
        if isinstance(value, (str, int, float, bool, type(None))):
            if isinstance(value, str):
                # Check if it's a path reference
                if value.startswith("$event.") or value.startswith("$trigger."):
                    # Validate the path format
                    if not _PATH.match(value[1:]):
                        raise MappingError(f"Invalid path reference: {value!r}")
                # Check for dangerous patterns in literals
                elif any(keyword in value.lower() for keyword in ("import", "exec", "eval", "compile", "__")):
                    raise MappingError(f"Potentially dangerous literal: {value!r}")
            continue
        if isinstance(value, dict):
            _validate_mapping(value)
            continue
        if isinstance(value, list):
            for item in value:
                if isinstance(item, (str, int, float, bool, type(None))):
                    if isinstance(item, str):
                        if item.startswith("$event.") or item.startswith("$trigger."):
                            if not _PATH.match(item[1:]):
                                raise MappingError(f"Invalid path reference: {item!r}")
                        elif any(keyword in item.lower() for keyword in ("import", "exec", "eval", "compile", "__")):
                            raise MappingError(f"Potentially dangerous literal: {item!r}")
                    continue
                if isinstance(item, dict):
                    _validate_mapping(item)
                    continue
                raise MappingError(f"Mapping value {value!r} is not a literal or dict")
            continue
        raise MappingError(f"Mapping value {value!r} is not a literal, dict, or list")


def apply_mapping(
    mapping: dict[str, Any],
    event: dict[str, Any],
    trigger: db.TriggerRecord,
) -> dict[str, Any]:
    """Apply a mapping to an event, substituting event and trigger references.

    Mapping values starting with "$event." or "$trigger." are resolved as paths.
    All other values are treated as literals.

    Returns a new dictionary with resolved values. Unknown paths fail closed
    by returning None for that field.
    """
    _validate_mapping(mapping)

    scope = {
        "event": event,
        "trigger": {
            "key": trigger.key,
            "name": trigger.name,
            "source_key": trigger.source_key,
            "event_type": trigger.event_type,
        },
    }

    def resolve(value: Any) -> Any:
        if isinstance(value, (str, int, float, bool, type(None))):
            if isinstance(value, str) and value.startswith("$event."):
                return _resolve_path(value[7:], scope["event"])
            if isinstance(value, str) and value.startswith("$trigger."):
                return _resolve_path(value[9:], scope["trigger"])
            return value
        if isinstance(value, dict):
            return {k: resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        return None

    return {k: resolve(v) for k, v in mapping.items()}


def verify_signature(
    payload: bytes,
    signature: str,
    secret: str,
    timestamp: int,
) -> bool:
    """Verify an HMAC-SHA256 signature with constant-time comparison.

    Returns False if:
    * timestamp is outside 5-minute window
    * signature does not match
    """
    now = int(time.time())
    if abs(now - timestamp) > 300:  # 5 minutes
        return False

    expected = hmac.new(
        secret.encode(),
        f"{timestamp}.{payload.decode()}".encode(),
        hashlib.sha256,
    ).hexdigest()

    if not signature.startswith("sha256="):
        return False

    provided = signature[7:]
    if len(expected) != len(provided):
        return False

    result = 0
    for a, b in zip(expected.encode(), provided.encode()):
        result |= a ^ b
    return result == 0


def generate_event_id() -> str:
    """Generate a unique event ID for deduplication."""
    return secrets.token_urlsafe(32)


def schedule_event_id(trigger_key: str, window_start: datetime) -> str:
    """Generate a deterministic event ID for scheduled triggers.

    Format: schedule:<trigger_key>:<window_start_iso>
    """
    return f"schedule:{trigger_key}:{window_start.isoformat()}"


def lease_trigger(
    session: Session,
    trigger_key: str,
    lease_duration_seconds: int = 60,
) -> Optional[str]:
    """Acquire a lease on a trigger to prevent double-firing.

    Returns the lease token if acquired, None if already leased.
    """
    trigger = session.get(db.TriggerRecord, trigger_key)
    if trigger is None or not trigger.active:
        return None

    now = db.utcnow()
    lease_expires = trigger.last_fired_at or trigger.created_at
    if lease_expires.tzinfo is None:
        lease_expires = lease_expires.replace(tzinfo=timezone.utc)

    if lease_expires + timedelta(seconds=lease_duration_seconds) > now:
        return None

    token = secrets.token_hex(16)
    trigger.last_fired_at = now
    session.flush()
    return token


def record_delivery(
    session: Session,
    trigger_key: str,
    event_id: str,
    payload: dict[str, Any],
    status: str = "received",
    request_id: Optional[str] = None,
    error: Optional[str] = None,
) -> db.TriggerDeliveryRecord:
    """Record a trigger delivery, handling deduplication.

    If a delivery with the same (trigger_key, event_id) exists, return it
    instead of creating a duplicate.
    """
    existing = session.execute(
        select(db.TriggerDeliveryRecord).where(
            db.TriggerDeliveryRecord.trigger_key == trigger_key,
            db.TriggerDeliveryRecord.event_id == event_id,
        )
    ).scalar_one_or_none()

    if existing:
        return existing

    delivery = db.TriggerDeliveryRecord(
        trigger_key=trigger_key,
        event_id=event_id,
        payload_json=json.dumps(payload, separators=(",", ":")),
        status=status,
        request_id=request_id,
        error=error,
        received_at=db.utcnow(),
    )
    session.add(delivery)
    session.flush()
    return delivery


def update_delivery(
    session: Session,
    delivery: db.TriggerDeliveryRecord,
    status: str,
    request_id: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Update a delivery's status and optional fields."""
    delivery.status = status
    if request_id is not None:
        delivery.request_id = request_id
    if error is not None:
        delivery.error = error
    delivery.processed_at = db.utcnow()
    session.flush()


def is_due_interval(trigger: db.TriggerRecord, now: datetime) -> bool:
    """Check if an interval trigger is due to fire."""
    if trigger.schedule_kind != "interval" or trigger.interval_seconds is None:
        return False
    if trigger.last_fired_at is None:
        return True
    if trigger.last_fired_at.tzinfo is None:
        trigger.last_fired_at = trigger.last_fired_at.replace(tzinfo=timezone.utc)
    return now >= trigger.last_fired_at + timedelta(seconds=trigger.interval_seconds)


def is_due_daily(trigger: db.TriggerRecord, now: datetime) -> bool:
    """Check if a daily trigger is due to fire."""
    if trigger.schedule_kind != "daily" or trigger.daily_at_utc is None:
        return False
    if trigger.last_fired_at is None:
        return True

    # Parse HH:MM UTC
    try:
        hour, minute = map(int, trigger.daily_at_utc.split(":"))
    except (ValueError, AttributeError):
        return False

    # Get today's target time in UTC
    today_target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if trigger.last_fired_at.tzinfo is None:
        trigger.last_fired_at = trigger.last_fired_at.replace(tzinfo=timezone.utc)

    # Fire if we haven't fired since today's target time
    return trigger.last_fired_at < today_target


def next_window_start(trigger: db.TriggerRecord, now: datetime) -> datetime:
    """Calculate the next scheduling window start for a trigger."""
    if trigger.schedule_kind == "interval" and trigger.interval_seconds:
        if trigger.last_fired_at is None:
            return now
        if trigger.last_fired_at.tzinfo is None:
            trigger.last_fired_at = trigger.last_fired_at.replace(tzinfo=timezone.utc)
        return trigger.last_fired_at + timedelta(seconds=trigger.interval_seconds)

    if trigger.schedule_kind == "daily" and trigger.daily_at_utc:
        try:
            hour, minute = map(int, trigger.daily_at_utc.split(":"))
        except (ValueError, AttributeError):
            return now
        today_target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now < today_target:
            return today_target
        return today_target + timedelta(days=1)

    return now


@dataclass
class ScheduledTick:
    """One trigger that fired during a scheduler tick."""

    trigger_key: str
    event_id: str
    window_start: datetime


def tick_scheduler(
    session: Session,
    now: Optional[datetime] = None,
) -> list[ScheduledTick]:
    """Find due triggers, acquire leases, and return fired events.

    This is the core scheduler loop. Two concurrent schedulers will not
    double-fire because lease_trigger uses last_fired_at as a fence.
    """
    if now is None:
        now = db.utcnow()

    triggers = session.execute(
        select(db.TriggerRecord).where(
            db.TriggerRecord.active == True,
            db.TriggerRecord.schedule_kind != "webhook",
        )
    ).scalars().all()

    ticks: list[ScheduledTick] = []
    for trigger in triggers:
        if trigger.schedule_kind == "interval" and not is_due_interval(trigger, now):
            continue
        if trigger.schedule_kind == "daily" and not is_due_daily(trigger, now):
            continue

        token = lease_trigger(session, trigger.key)
        if token is None:
            continue

        window_start = next_window_start(trigger, now)
        event_id = schedule_event_id(trigger.key, window_start)
        ticks.append(ScheduledTick(trigger_key=trigger.key, event_id=event_id, window_start=window_start))

    session.commit()
    return ticks


# ---------------------------------------------------------------------------
# CLI entrypoint


def main_once() -> None:
    """Run a single scheduler tick and exit."""
    from src import db as db_module

    db_module._ensure_tables()
    session = db_module.SessionLocal()
    try:
        ticks = tick_scheduler(session)
        print(f"Scheduled {len(ticks)} trigger(s)")
        for tick in ticks:
            print(f"  {tick.trigger_key}: {tick.event_id}")
    finally:
        session.close()


def main_poll(interval_seconds: int = 2) -> None:
    """Run the scheduler in a poll loop."""
    import time

    from src import db as db_module

    db_module._ensure_tables()
    while True:
        session = db_module.SessionLocal()
        try:
            ticks = tick_scheduler(session)
            if ticks:
                print(f"[{db_module.utcnow().isoformat()}] Scheduled {len(ticks)} trigger(s)")
        finally:
            session.close()
        time.sleep(interval_seconds)


if __name__ == "__main__":
    import sys

    if "--once" in sys.argv:
        main_once()
    elif "--poll" in sys.argv:
        interval = 2
        for i, arg in enumerate(sys.argv):
            if arg == "--interval" and i + 1 < len(sys.argv):
                try:
                    interval = int(sys.argv[i + 1])
                except ValueError:
                    pass
        main_poll(interval)
    else:
        print("Usage: python -m src.triggers --once | --poll [--interval SECONDS]")
        sys.exit(1)
