"""Monitoring and notification routes (extracted from src/api.py).

Read-only delivery visibility, the notification dead-letter workflow, and the
execution-log/audit trail. The route bodies, response models, status codes,
and error semantics are unchanged; ``scripts/dump_openapi.py`` plus the
committed snapshot in ``tests/openapi_snapshot.json`` keep the OpenAPI
surface byte-identical across this decomposition.
"""

import json
import os
import time
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from src import db
from src.auth import Actor
from src.deps import _admin_actor, _new_run_id, _operator_actor, _scope_execution_log_query, _scope_ticket_or_request_keyed_query
from src.notify import deliver_webhook
from src.schemas import ExecutionLogOut, NotificationOutboxOut
from src.services.connectors import deliver_outbox_notification

router = APIRouter()

DEFAULT_NOTIFICATION_MAX_ATTEMPTS = 5


def _notification_max_attempts() -> int:
    """Read the bounded dead-letter threshold for outbox deliveries."""
    try:
        configured = int(
            os.environ.get(
                "FLOWOPS_NOTIFICATION_MAX_ATTEMPTS",
                DEFAULT_NOTIFICATION_MAX_ATTEMPTS,
            )
        )
        return max(1, min(20, configured))
    except (TypeError, ValueError):
        return DEFAULT_NOTIFICATION_MAX_ATTEMPTS


def _outbox_out(record: db.NotificationOutboxRecord) -> NotificationOutboxOut:
    return NotificationOutboxOut(
        **{c: getattr(record, c) for c in (
            "id", "url", "ticket_id", "status", "attempts",
            "last_error", "created_at",
        )}
    )


class ConsoleDeliveryOut(BaseModel):
    id: int
    ticket_id: str | None = None
    status: str
    attempts: int
    created_at: datetime


@router.get("/command-center/deliveries", response_model=list[ConsoleDeliveryOut])
def console_deliveries(
    state: Literal["pending", "failed"] = Query(default="failed"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> list[ConsoleDeliveryOut]:
    """Tenant-scoped delivery visibility without destination URLs or raw errors."""
    query = _scope_ticket_or_request_keyed_query(
        select(db.NotificationOutboxRecord).where(db.NotificationOutboxRecord.status == state),
        db.NotificationOutboxRecord.ticket_id, actor,
    )
    rows = session.execute(query.order_by(db.NotificationOutboxRecord.id.desc()).offset(offset).limit(limit)).scalars()
    return [ConsoleDeliveryOut.model_validate(row, from_attributes=True) for row in rows]


@router.get("/notifications/pending", response_model=list[NotificationOutboxOut])
def list_pending_notifications(
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[NotificationOutboxOut]:
    """List webhook deliveries that await a manual retry."""
    query = _scope_ticket_or_request_keyed_query(
        select(db.NotificationOutboxRecord).where(
            db.NotificationOutboxRecord.status == "pending"
        ),
        db.NotificationOutboxRecord.ticket_id,
        actor,
    )
    records = session.execute(
        query
        .order_by(db.NotificationOutboxRecord.id)
        .offset(offset)
        .limit(limit)
    ).scalars().all()
    return [_outbox_out(record) for record in records]


@router.get("/notifications/failed", response_model=list[NotificationOutboxOut])
def list_failed_notifications(
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[NotificationOutboxOut]:
    """List dead-letter deliveries that exhausted their retry budget.

    Operators can inspect these rows and return one to the pending queue with
    ``POST /notifications/{notification_id}/requeue``.
    """
    query = _scope_ticket_or_request_keyed_query(
        select(db.NotificationOutboxRecord).where(
            db.NotificationOutboxRecord.status == "failed"
        ),
        db.NotificationOutboxRecord.ticket_id,
        actor,
    )
    records = session.execute(
        query
        .order_by(db.NotificationOutboxRecord.id)
        .offset(offset)
        .limit(limit)
    ).scalars().all()
    return [_outbox_out(record) for record in records]


@router.post("/notifications/retry", response_model=list[NotificationOutboxOut])
def retry_failed_notifications(
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[NotificationOutboxOut]:
    """Retry pending webhook deliveries once each (oldest first).

    Successful retries are marked ``delivered``; still-failing ones stay
    ``pending`` until their attempt count reaches
    ``FLOWOPS_NOTIFICATION_MAX_ATTEMPTS``, at which point they become
    ``failed`` (dead-lettered). Every outcome is written to the execution log.
    """
    max_attempts = _notification_max_attempts()
    query = _scope_ticket_or_request_keyed_query(
        select(db.NotificationOutboxRecord).where(
            db.NotificationOutboxRecord.status == "pending"
        ),
        db.NotificationOutboxRecord.ticket_id,
        actor,
    )
    records = session.execute(
        query
        .order_by(db.NotificationOutboxRecord.id)
        .limit(limit)
    ).scalars().all()
    results = []
    for record in records:
        payload = json.loads(record.payload_json)
        started = time.perf_counter()
        # One bounded retry per call; the operator can call again later.
        if payload.get("event") == "trigger.fired" and payload.get("tenant"):
            delivery = deliver_outbox_notification(
                record.url, payload, session, max_attempts=1
            )
        else:
            delivery = deliver_webhook(record.url, payload, max_attempts=1)
        duration_ms = int((time.perf_counter() - started) * 1000)
        record.attempts += delivery.attempts
        if delivery.success:
            record.status = "delivered"
            record.last_error = None
        else:
            record.last_error = delivery.last_error
            if record.attempts >= max_attempts:
                record.status = "failed"
                db.log_execution(
                    session,
                    run_id=_new_run_id(),
                    action="notification.dead_lettered",
                    status="failure",
                    ticket_id=record.ticket_id,
                    duration_ms=duration_ms,
                    detail=(
                        f"outbox #{record.id} exhausted after "
                        f"{record.attempts} attempts"
                    ),
                    actor_subject=actor.subject,
                )
        db.log_execution(
            session,
            run_id=_new_run_id(),
            action="notification.retry",
            status="success" if delivery.success else "failure",
            ticket_id=record.ticket_id,
            duration_ms=duration_ms,
            detail=f"outbox #{record.id}: now '{record.status}'",
            actor_subject=actor.subject,
        )
        results.append(_outbox_out(record))
    return results


@router.post(
    "/notifications/{notification_id}/requeue",
    response_model=NotificationOutboxOut,
)
def requeue_failed_notification(
    notification_id: int,
    actor: Actor = Depends(_admin_actor),
    session: Session = Depends(db.get_session),
) -> NotificationOutboxOut:
    """Return one dead-letter delivery to the pending queue.

    The transition is conditional on the row still being ``failed``, so two
    operators cannot requeue the same delivery twice. Attempt history and the
    last error are preserved; each requeue grants one more delivery attempt.
    """
    record = session.execute(
        _scope_ticket_or_request_keyed_query(
            select(db.NotificationOutboxRecord).where(
                db.NotificationOutboxRecord.id == notification_id
            ),
            db.NotificationOutboxRecord.ticket_id,
            actor,
        )
    ).scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail="Notification delivery not found.")
    if record.status != "failed":
        raise HTTPException(
            status_code=409,
            detail=f"Only failed deliveries can be requeued (current status: '{record.status}').",
        )

    table = db.NotificationOutboxRecord.__table__
    now = db.utcnow()
    result = session.execute(
        update(table)
        .where(
            and_(
                table.c.id == notification_id,
                table.c.status == "failed",
            )
        )
        .values(status="pending", updated_at=now)
    )
    if result.rowcount != 1:
        raise HTTPException(
            status_code=409,
            detail="Notification delivery changed state; refresh and retry.",
        )
    session.expire_all()
    record = session.get(db.NotificationOutboxRecord, notification_id)
    if record is None:  # pragma: no cover - only if deleted concurrently
        raise HTTPException(status_code=404, detail="Notification delivery not found.")
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action="notification.requeued",
        status="success",
        ticket_id=record.ticket_id,
        duration_ms=0,
        detail=(
            f"outbox #{record.id} returned to pending; "
            f"attempts={record.attempts} preserved"
        ),
        actor_subject=actor.subject,
    )
    return _outbox_out(record)


@router.get("/execution-logs", response_model=list[ExecutionLogOut])
def list_execution_logs(
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
    status_filter: str | None = Query(default=None, alias="status"),
    actor_filter: str | None = Query(default=None, alias="actor"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ExecutionLogOut]:
    """Recent workflow execution log entries, newest first."""
    statement = _scope_execution_log_query(
        select(db.ExecutionLogRecord), actor
    ).order_by(db.ExecutionLogRecord.id.desc())
    if status_filter in ("success", "failure"):
        statement = statement.where(db.ExecutionLogRecord.status == status_filter)
    if actor_filter:
        statement = statement.where(
            db.ExecutionLogRecord.actor_subject == actor_filter
        )
    records = session.execute(statement.offset(offset).limit(limit)).scalars().all()
    return [
        ExecutionLogOut(
            **{c: getattr(record, c) for c in (
                "id", "run_id", "action", "status", "ticket_id",
                "duration_ms", "detail", "actor_subject", "correlation_id", "created_at",
            )}
        )
        for record in records
    ]


@router.get("/audit", response_model=list[ExecutionLogOut])
def list_audit_trail(
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
    status_filter: str | None = Query(default=None, alias="status"),
    actor_filter: str | None = Query(default=None, alias="actor"),
    action: str | None = Query(default=None, max_length=64),
    ticket_id: str | None = Query(default=None, max_length=64),
    created_from: datetime | None = Query(default=None, alias="from"),
    created_to: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ExecutionLogOut]:
    """Recent execution rows that carry an authenticated actor."""
    query = _scope_execution_log_query(
        select(db.ExecutionLogRecord).where(
            db.ExecutionLogRecord.actor_subject.is_not(None)
        ),
        actor,
    )
    if status_filter in ("success", "failure"):
        query = query.where(db.ExecutionLogRecord.status == status_filter)
    if actor_filter:
        query = query.where(
            db.ExecutionLogRecord.actor_subject == actor_filter
        )
    if action:
        query = query.where(db.ExecutionLogRecord.action.startswith(action, autoescape=True))
    if ticket_id:
        query = query.where(db.ExecutionLogRecord.ticket_id == ticket_id)
    if created_from is not None:
        query = query.where(db.ExecutionLogRecord.created_at >= created_from)
    if created_to is not None:
        query = query.where(db.ExecutionLogRecord.created_at < created_to)
    records = session.execute(
        query.order_by(db.ExecutionLogRecord.id.desc()).offset(offset).limit(limit)
    ).scalars().all()
    return [
        ExecutionLogOut(
            **{c: getattr(record, c) for c in (
                "id", "run_id", "action", "status", "ticket_id",
                "duration_ms", "detail", "actor_subject", "correlation_id", "created_at",
            )}
        )
        for record in records
    ]
