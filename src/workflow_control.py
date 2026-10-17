"""Workflow control-plane service for durable job orchestration.

This module is the single API-facing boundary for queue operations. The
worker retains low-level lease mechanics in ``src.jobs``; HTTP handlers use
these functions so authorization, correlation IDs, and audit logging stay
consistent.
"""

import uuid
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src import db, jobs


def correlation_id() -> str:
    return uuid.uuid4().hex[:16]


def enqueue_onboarding_item(
    session: Session,
    *,
    request_id: int,
    item_id: int,
    action: str,
    label: str,
    actor_subject: Optional[str],
    correlation: Optional[str] = None,
) -> db.WorkflowJobRecord:
    """Enqueue one onboarding action and audit the control-plane transition."""
    correlation = correlation or correlation_id()
    job = jobs.enqueue_job(
        session,
        job_type=jobs.JOB_ONBOARDING_ITEM,
        payload={
            "request_id": request_id,
            "item_id": item_id,
            "action": action,
            "correlation_id": correlation,
        },
        idempotency_key=f"onboarding:{request_id}:{item_id}",
        resource_type="onboarding",
        resource_id=str(request_id),
        label=label,
    )
    db.log_execution(
        session,
        run_id=correlation,
        action="workflow.job.enqueued",
        status="success",
        ticket_id=str(request_id),
        duration_ms=0,
        detail=f"job #{job.id} ({action}) queued for item #{item_id}",
        actor_subject=actor_subject,
        correlation_id=correlation,
    )
    return job


def get_job(session: Session, job_id: int) -> db.WorkflowJobRecord:
    job = session.get(db.WorkflowJobRecord, job_id)
    if job is None:
        raise LookupError(f"Workflow job {job_id} was not found.")
    return job


def retry(session: Session, job_id: int, actor_subject: Optional[str]) -> db.WorkflowJobRecord:
    job = get_job(session, job_id)
    if not jobs.retry_failed_job(session, job_id):
        raise ValueError(f"Workflow job {job_id} is not failed.")
    db.log_execution(
        session, run_id=correlation_id(), action="workflow.job.retried", status="success",
        ticket_id=job.resource_id, duration_ms=0, detail=f"job #{job_id} returned to queue",
        actor_subject=actor_subject,
    )
    session.flush()
    return get_job(session, job_id)


def cancel(session: Session, job_id: int, actor_subject: Optional[str]) -> db.WorkflowJobRecord:
    job = get_job(session, job_id)
    if not jobs.cancel_job(session, job_id):
        raise ValueError(f"Workflow job {job_id} cannot be cancelled from '{job.status}'.")
    db.log_execution(
        session, run_id=correlation_id(), action="workflow.job.cancelled", status="success",
        ticket_id=job.resource_id, duration_ms=0, detail=f"job #{job_id} cancelled",
        actor_subject=actor_subject,
    )
    session.flush()
    return get_job(session, job_id)


def history(session: Session, job_id: int) -> list[db.ActionExecutionRecord]:
    """Return durable attempts associated with a job's onboarding item."""
    job = get_job(session, job_id)
    payload = __import__("json").loads(job.payload_json)
    return list(session.execute(
        select(db.ActionExecutionRecord)
        .where(db.ActionExecutionRecord.checklist_item_id == int(payload["item_id"]))
        .order_by(db.ActionExecutionRecord.id)
    ).scalars().all())
