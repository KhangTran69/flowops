"""Durable trigger delivery worker (v1.4).

Processes ``trigger_deliveries`` rows: applies the trigger's mapping to the
event payload, creates a workflow request or ticket through the existing
paths, and records the outcome. Uses the same lease/fencing pattern as the
workflow job worker so a crash between delivery insert and workflow creation
cannot lose the event or create it twice.

Run once (useful in tests):

    python -m src.trigger_worker --once

Or as a polling daemon:

    python -m src.trigger_worker --poll --interval 2
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from src import db
from src.trigger_mapping import MappingError, apply_mapping

JOB_TRIGGER_EXECUTE = "trigger.execute"
DEFAULT_LEASE_SECONDS = 60
MAX_BACKOFF_SECONDS = 300
DEFAULT_MAX_ATTEMPTS = 5


class PermanentDeliveryError(Exception):
    """A delivery failure that should not be retried."""


def backoff_seconds(attempts: int) -> int:
    return min(MAX_BACKOFF_SECONDS, 2 ** max(0, attempts - 1))


def _claimable(now: datetime):
    return or_(
        and_(
            db.TriggerDeliveryRecord.status == "received",
        ),
        and_(
            db.TriggerDeliveryRecord.status == "queued",
            db.TriggerDeliveryRecord.available_at.is_not(None),
            db.TriggerDeliveryRecord.available_at <= now,
        ),
        and_(
            db.TriggerDeliveryRecord.status == "running",
            db.TriggerDeliveryRecord.lease_expires_at.is_not(None),
            db.TriggerDeliveryRecord.lease_expires_at < now,
        ),
    )


def claim_delivery(
    session: Session,
    *,
    worker_id: Optional[str] = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: Optional[datetime] = None,
) -> Optional[db.TriggerDeliveryRecord]:
    """Atomically claim one due delivery."""
    at = now or db.utcnow()
    token = f"{worker_id or 'worker'}:{uuid.uuid4().hex[:16]}"
    table = db.TriggerDeliveryRecord
    candidate_query = (
        select(table.id)
        .where(_claimable(at))
        .order_by(table.received_at, table.id)
        .limit(10)
    )
    if session.get_bind().dialect.name == "postgresql":
        candidate_query = candidate_query.with_for_update(skip_locked=True)
    candidate_ids = session.execute(candidate_query).scalars().all()

    for delivery_id in candidate_ids:
        result = session.execute(
            update(table)
            .execution_options(synchronize_session=False)
            .where(and_(table.id == delivery_id, _claimable(at)))
            .values(
                status="running",
                attempts=table.attempts + 1,
                lease_owner_token=token,
                lease_expires_at=at + timedelta(seconds=lease_seconds),
                available_at=None,
                updated_at=at,
            )
        )
        if result.rowcount == 1:
            session.commit()
            session.expire_all()
            return session.get(table, delivery_id)
    session.commit()
    return None


def _fail_delivery(
    session: Session,
    delivery: db.TriggerDeliveryRecord,
    error: str,
    *,
    retryable: bool = True,
    now: Optional[datetime] = None,
) -> str:
    """Mark a delivery failed or schedule a retry, fenced on the owner token."""
    at = now or db.utcnow()
    table = db.TriggerDeliveryRecord
    terminal = not retryable or delivery.attempts >= delivery.max_attempts
    if terminal:
        values = {
            "status": "failed",
            "error": error[:2000],
            "lease_owner_token": None,
            "lease_expires_at": None,
            "processed_at": at,
            "available_at": None,
            "updated_at": at,
        }
        resulting_status = "failed"
    else:
        delay = backoff_seconds(delivery.attempts)
        values = {
            "status": "queued",
            "error": error[:2000],
            "lease_owner_token": None,
            "lease_expires_at": None,
            "available_at": at + timedelta(seconds=delay),
            "updated_at": at,
        }
        resulting_status = "queued"
    outcome = session.execute(
        update(table)
        .execution_options(synchronize_session=False)
        .where(
            and_(
                table.id == delivery.id,
                table.lease_owner_token == delivery.lease_owner_token,
                table.status == "running",
            )
        )
        .values(**values)
    )
    return resulting_status if outcome.rowcount == 1 else "fenced"


def _succeed_delivery(
    session: Session,
    delivery: db.TriggerDeliveryRecord,
    request_id: str,
    *,
    now: Optional[datetime] = None,
) -> bool:
    at = now or db.utcnow()
    table = db.TriggerDeliveryRecord
    outcome = session.execute(
        update(table)
        .execution_options(synchronize_session=False)
        .where(
            and_(
                table.id == delivery.id,
                table.lease_owner_token == delivery.lease_owner_token,
                table.status == "running",
            )
        )
        .values(
            status="succeeded",
            request_id=request_id,
            error=None,
            lease_owner_token=None,
            lease_expires_at=None,
            processed_at=at,
            available_at=None,
            updated_at=at,
        )
    )
    return outcome.rowcount == 1


def _skip_delivery(
    session: Session,
    delivery: db.TriggerDeliveryRecord,
    reason: str,
    *,
    now: Optional[datetime] = None,
) -> bool:
    at = now or db.utcnow()
    table = db.TriggerDeliveryRecord
    outcome = session.execute(
        update(table)
        .execution_options(synchronize_session=False)
        .where(
            and_(
                table.id == delivery.id,
                table.lease_owner_token == delivery.lease_owner_token,
                table.status == "running",
            )
        )
        .values(
            status="skipped",
            error=reason[:2000],
            lease_owner_token=None,
            lease_expires_at=None,
            processed_at=at,
            available_at=None,
            updated_at=at,
        )
    )
    return outcome.rowcount == 1


def _process_delivery(
    session: Session,
    delivery: db.TriggerDeliveryRecord,
    *,
    now: Optional[datetime] = None,
) -> str:
    """Apply the mapping and create the target workflow/ticket.

    Returns the resulting status: ``succeeded``, ``failed``, ``skipped``,
    ``queued`` (retry), or ``fenced``.
    """
    at = now or db.utcnow()
    trigger = session.get(db.TriggerRecord, delivery.trigger_key)
    if trigger is None or not trigger.active:
        _skip_delivery(session, delivery, "trigger not found or inactive", now=at)
        db.log_execution(
            session,
            run_id=uuid.uuid4().hex[:12],
            action="trigger.delivery.skipped",
            status="success",
            detail=f"delivery #{delivery.id}: trigger inactive or missing",
            actor_subject=f"trigger:{delivery.trigger_key}",
        )
        session.commit()
        return "skipped"

    try:
        event_payload = json.loads(delivery.payload_json)
    except (TypeError, ValueError) as error:
        _fail_delivery(session, delivery, f"invalid payload JSON: {error}", retryable=False, now=at)
        db.log_execution(
            session,
            run_id=uuid.uuid4().hex[:12],
            action="trigger.delivery.failed",
            status="failure",
            detail=f"delivery #{delivery.id}: invalid payload",
            actor_subject=f"trigger:{delivery.trigger_key}",
        )
        session.commit()
        return "failed"

    trigger_fields = {
        "key": trigger.key,
        "name": trigger.name,
        "source_key": trigger.source_key,
        "event_type": trigger.event_type,
        "target_type": trigger.target_type,
        "definition_key": trigger.definition_key,
    }

    try:
        mapped = apply_mapping(trigger.mapping_json, event_payload, trigger_fields)
    except MappingError as error:
        _fail_delivery(session, delivery, str(error), retryable=False, now=at)
        db.log_execution(
            session,
            run_id=uuid.uuid4().hex[:12],
            action="trigger.delivery.failed",
            status="failure",
            detail=f"delivery #{delivery.id}: mapping error: {error}",
            actor_subject=f"trigger:{delivery.trigger_key}",
        )
        session.commit()
        return "failed"

    # Idempotency key ensures a crash between delivery insert and workflow
    # creation cannot create two requests.
    idempotency_key = f"trigger:{delivery.trigger_key}:{delivery.event_id}"

    try:
        request_id = _create_target(session, trigger, mapped, idempotency_key, delivery, at)
    except PermanentDeliveryError as error:
        _fail_delivery(session, delivery, str(error), retryable=False, now=at)
        db.log_execution(
            session,
            run_id=uuid.uuid4().hex[:12],
            action="trigger.delivery.failed",
            status="failure",
            detail=f"delivery #{delivery.id}: {error}",
            actor_subject=f"trigger:{delivery.trigger_key}",
        )
        session.commit()
        return "failed"
    except Exception as error:
        session.rollback()
        reloaded = session.get(db.TriggerDeliveryRecord, delivery.id)
        if reloaded is None:
            return "missing"
        status = _fail_delivery(session, reloaded, str(error), retryable=True, now=at)
        db.log_execution(
            session,
            run_id=uuid.uuid4().hex[:12],
            action="trigger.delivery.failed",
            status="failure",
            detail=f"delivery #{delivery.id}: transient error: {error}",
            actor_subject=f"trigger:{delivery.trigger_key}",
        )
        session.commit()
        return status or "fenced"

    if not _succeed_delivery(session, delivery, request_id, now=at):
        session.rollback()
        return "fenced"

    db.log_execution(
        session,
        run_id=uuid.uuid4().hex[:12],
        action="trigger.delivery.succeeded",
        status="success",
        detail=f"delivery #{delivery.id}: created {trigger.target_type} {request_id}",
        actor_subject=f"trigger:{delivery.trigger_key}",
    )
    session.commit()
    return "succeeded"


def _create_target(
    session: Session,
    trigger: db.TriggerRecord,
    mapped: dict[str, Any],
    idempotency_key: str,
    delivery: db.TriggerDeliveryRecord,
    now: datetime,
) -> str:
    """Create the target workflow request or ticket.

    Returns the request_id as a string. Raises PermanentDeliveryError for
    non-retryable failures.
    """
    if trigger.target_type in ("onboarding_request", "offboarding_request"):
        return _create_onboarding(session, trigger, mapped, idempotency_key, now)
    elif trigger.target_type == "ticket":
        return _create_ticket(session, trigger, mapped, idempotency_key, now)
    else:
        raise PermanentDeliveryError(f"Unknown target_type: {trigger.target_type!r}")


def _create_onboarding(
    session: Session,
    trigger: db.TriggerRecord,
    mapped: dict[str, Any],
    idempotency_key: str,
    now: datetime,
) -> str:
    """Create an onboarding/offboarding request through the existing path."""
    from src.api import OnboardingRequestIn, _create_onboarding_request
    from src.onboarding import checklist_for, initial_status_for

    request_type = "onboarding" if trigger.target_type == "onboarding_request" else "offboarding"

    # Validate required fields.
    required = ("employee_name", "employee_email", "department", "requester_id")
    missing = [f for f in required if f not in mapped or mapped[f] is None]
    if missing:
        raise PermanentDeliveryError(f"Missing required fields: {', '.join(missing)}")

    # Check for existing idempotency record (replay after crash).
    existing = session.execute(
        select(db.IdempotencyRecord).where(
            db.IdempotencyRecord.key == idempotency_key
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing.ticket_id  # ticket_id column stores the request_id

    request = OnboardingRequestIn(
        request_type=request_type,
        employee_name=str(mapped["employee_name"]),
        employee_email=str(mapped["employee_email"]),
        department=str(mapped["department"]),
        requester_id=int(mapped["requester_id"]),
        start_date=mapped.get("start_date"),
        definition_key=trigger.definition_key,
    )
    result = _create_onboarding_request(request, session, actor_subject=f"trigger:{trigger.key}")
    session.flush()

    # Store idempotency record.
    session.add(
        db.IdempotencyRecord(
            key=idempotency_key,
            ticket_id=str(result.id),
            response_json=json.dumps(
                {"onboarding_request": result.model_dump(mode="json")},
                separators=(",", ":"),
                default=str,
            ),
            resource_type="onboarding",
        )
    )
    session.flush()
    return str(result.id)


def _create_ticket(
    session: Session,
    trigger: db.TriggerRecord,
    mapped: dict[str, Any],
    idempotency_key: str,
    now: datetime,
) -> str:
    """Create a ticket through the existing intake/routing path."""
    from src.api import TicketIntakeRequest, _process_intake

    required = ("userId", "id", "title", "completed")
    missing = [f for f in required if f not in mapped or mapped[f] is None]
    if missing:
        raise PermanentDeliveryError(f"Missing required fields: {', '.join(missing)}")

    existing = session.execute(
        select(db.IdempotencyRecord).where(
            db.IdempotencyRecord.key == idempotency_key
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing.ticket_id

    request = TicketIntakeRequest(
        userId=int(mapped["userId"]),
        id=int(mapped["id"]),
        title=str(mapped["title"]),
        completed=bool(mapped["completed"]),
    )
    result = _process_intake(request, session, actor_subject=f"trigger:{trigger.key}")
    session.flush()

    session.add(
        db.IdempotencyRecord(
            key=idempotency_key,
            ticket_id=result.ticket_id,
            response_json=json.dumps(
                {"ticket": result.model_dump()},
                separators=(",", ":"),
                default=str,
            ),
            resource_type="ticket",
        )
    )
    session.flush()
    return result.ticket_id


def run_once(
    *,
    worker_id: Optional[str] = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: Optional[datetime] = None,
) -> Optional[db.TriggerDeliveryRecord]:
    """Claim and process at most one due delivery."""
    db._ensure_tables()
    session = db.SessionLocal()
    try:
        delivery = claim_delivery(
            session, worker_id=worker_id, lease_seconds=lease_seconds, now=now
        )
        if delivery is None:
            return None
        _process_delivery(session, delivery, now=now)
        session.expire_all()
        return session.get(db.TriggerDeliveryRecord, delivery.id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="FlowOps trigger delivery worker")
    parser.add_argument("--once", action="store_true", help="process at most one delivery")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="seconds between empty polls")
    parser.add_argument("--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS)
    parser.add_argument("--worker-id", default=None)
    args = parser.parse_args(argv)
    worker_id = args.worker_id or f"trigger-worker-{uuid.uuid4().hex[:8]}"
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    import signal

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while not stopping:
        delivery = run_once(
            worker_id=worker_id,
            lease_seconds=args.lease_seconds,
        )
        if delivery is not None:
            print(f"{delivery.id} {delivery.status} {delivery.trigger_key}", flush=True)
            if args.once:
                return 0
        elif args.once:
            return 0
        if not args.once:
            time.sleep(max(0.1, args.poll_interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
