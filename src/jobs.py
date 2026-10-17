"""Durable workflow jobs: enqueue, leased claim, retry, and completion.

The lease/fencing model mirrors the idempotency reservations: a claim gets a
unique owner token and a lease deadline, and every terminal transition is a
conditional update on that token. A stale worker cannot finish or reschedule a
job after another worker has taken it over.
"""

import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from src import db

JOB_ONBOARDING_ITEM = "onboarding.item.execute"

JOB_STATUSES = (
    "queued",
    "running",
    "succeeded",
    "retry_scheduled",
    "failed",
    "cancelled",
)
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_LEASE_SECONDS = 60
MAX_BACKOFF_SECONDS = 300


def backoff_seconds(attempts: int) -> int:
    """Exponential backoff capped at five minutes."""
    return min(MAX_BACKOFF_SECONDS, 2 ** max(0, attempts - 1))


def enqueue_job(
    session: Session,
    *,
    job_type: str,
    payload: dict[str, Any],
    idempotency_key: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    label: Optional[str] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> db.WorkflowJobRecord:
    """Insert a queued job, deduplicating by idempotency key when supplied."""
    if idempotency_key:
        existing = session.execute(
            select(db.WorkflowJobRecord).where(
                db.WorkflowJobRecord.idempotency_key == idempotency_key
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
    job = db.WorkflowJobRecord(
        job_type=job_type,
        payload_json=json.dumps(payload, separators=(",", ":")),
        idempotency_key=idempotency_key,
        status="queued",
        attempts=0,
        max_attempts=max(1, max_attempts),
        available_at=db.utcnow(),
        resource_type=resource_type,
        resource_id=resource_id,
        label=label,
    )
    session.add(job)
    session.flush()
    return job


def _claimable(now: datetime):
    return or_(
        and_(
            db.WorkflowJobRecord.status.in_(("queued", "retry_scheduled")),
            db.WorkflowJobRecord.available_at <= now,
        ),
        and_(
            db.WorkflowJobRecord.status == "running",
            db.WorkflowJobRecord.lease_expires_at.is_not(None),
            db.WorkflowJobRecord.lease_expires_at < now,
        ),
    )


def claim_job(
    session: Session,
    worker_id: Optional[str] = None,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: Optional[datetime] = None,
    candidate_limit: int = 10,
) -> Optional[db.WorkflowJobRecord]:
    """Atomically claim the oldest due job for this worker."""
    now = now or db.utcnow()
    token = uuid.uuid4().hex
    table = db.WorkflowJobRecord
    candidate_query = (
        select(table.id)
        .where(_claimable(now))
        .order_by(table.available_at, table.id)
        .limit(candidate_limit)
    )
    if session.get_bind().dialect.name == "postgresql":
        candidate_query = candidate_query.with_for_update(skip_locked=True)
    candidate_ids = session.execute(candidate_query).scalars().all()

    for job_id in candidate_ids:
        result = session.execute(
            update(table).execution_options(synchronize_session=False)
            .where(and_(table.id == job_id, _claimable(now)))
            .values(
                status="running",
                attempts=table.attempts + 1,
                lease_owner_token=token,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                updated_at=now,
            )
        )
        if result.rowcount == 1:
            session.commit()
            session.expire_all()
            return session.get(table, job_id)
    session.commit()
    return None


def heartbeat_job(
    session: Session,
    job_id: int,
    owner_token: str,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: Optional[datetime] = None,
) -> bool:
    """Extend a running job lease only while this worker still owns it."""
    now = now or db.utcnow()
    table = db.WorkflowJobRecord
    result = session.execute(
        update(table).execution_options(synchronize_session=False)
        .where(
            and_(
                table.id == job_id,
                table.lease_owner_token == owner_token,
                table.status == "running",
            )
        )
        .values(
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            updated_at=now,
        )
    )
    return result.rowcount == 1


def complete_job(
    session: Session,
    job_id: int,
    owner_token: str,
    *,
    result: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Mark a job succeeded only if this worker still owns the lease."""
    now = now or db.utcnow()
    table = db.WorkflowJobRecord
    outcome = session.execute(
        update(table).execution_options(synchronize_session=False)
        .where(
            and_(
                table.id == job_id,
                table.lease_owner_token == owner_token,
                table.status == "running",
            )
        )
        .values(
            status="succeeded",
            result_json=json.dumps(result or {}, separators=(",", ":")),
            last_error=None,
            lease_owner_token=None,
            lease_expires_at=None,
            completed_at=now,
            updated_at=now,
        )
    )
    return outcome.rowcount == 1


def fail_job(
    session: Session,
    job_id: int,
    owner_token: str,
    error: str,
    *,
    retryable: bool = True,
    now: Optional[datetime] = None,
    delay_seconds: Optional[int] = None,
) -> Optional[str]:
    """Schedule a retry or dead-letter a job, fenced on the owner token.

    Returns the resulting status, or ``None`` if this worker no longer owns the
    job (for example after a stale-lease takeover).
    """
    now = now or db.utcnow()
    table = db.WorkflowJobRecord
    job = session.get(table, job_id)
    if job is None or job.lease_owner_token != owner_token or job.status != "running":
        return None
    terminal = not retryable or job.attempts >= job.max_attempts
    if terminal:
        values = {
            "status": "failed",
            "last_error": error,
            "lease_owner_token": None,
            "lease_expires_at": None,
            "completed_at": now,
            "updated_at": now,
        }
        resulting_status = "failed"
    else:
        delay = backoff_seconds(job.attempts) if delay_seconds is None else max(0, delay_seconds)
        values = {
            "status": "retry_scheduled",
            "last_error": error,
            "available_at": now + timedelta(seconds=delay),
            "lease_owner_token": None,
            "lease_expires_at": None,
            "updated_at": now,
        }
        resulting_status = "retry_scheduled"
    outcome = session.execute(
        update(table).execution_options(synchronize_session=False)
        .where(
            and_(
                table.id == job_id,
                table.lease_owner_token == owner_token,
                table.status == "running",
            )
        )
        .values(**values)
    )
    return resulting_status if outcome.rowcount == 1 else None


def retry_failed_job(
    session: Session, job_id: int, *, now: Optional[datetime] = None
) -> bool:
    """Operator action: return a failed job to the queue with a fresh budget."""
    now = now or db.utcnow()
    table = db.WorkflowJobRecord
    result = session.execute(
        update(table).execution_options(synchronize_session=False)
        .where(and_(table.id == job_id, table.status == "failed"))
        .values(
            status="queued",
            attempts=0,
            available_at=now,
            last_error=None,
            result_json=None,
            lease_owner_token=None,
            lease_expires_at=None,
            completed_at=None,
            updated_at=now,
        )
    )
    return result.rowcount == 1


def cancel_job(
    session: Session, job_id: int, *, now: Optional[datetime] = None
) -> bool:
    """Operator action: cancel a job that has not started yet."""
    now = now or db.utcnow()
    table = db.WorkflowJobRecord
    result = session.execute(
        update(table).execution_options(synchronize_session=False)
        .where(
            and_(
                table.id == job_id,
                table.status.in_(("queued", "retry_scheduled")),
            )
        )
        .values(
            status="cancelled",
            completed_at=now,
            lease_owner_token=None,
            lease_expires_at=None,
            updated_at=now,
        )
    )
    return result.rowcount == 1


def list_jobs(
    session: Session,
    *,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Iterable[db.WorkflowJobRecord]:
    statement = (
        select(db.WorkflowJobRecord)
        .order_by(db.WorkflowJobRecord.id.desc())
        .offset(max(0, offset))
        .limit(max(1, min(limit, 200)))
    )
    if status in JOB_STATUSES:
        statement = statement.where(db.WorkflowJobRecord.status == status)
    return session.execute(statement).scalars().all()
