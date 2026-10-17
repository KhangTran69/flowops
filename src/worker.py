"""Durable workflow worker for onboarding action execution.

Run once (useful in tests):

    python -m src.worker --once

Or as a polling daemon:

    python -m src.worker --poll-interval 2
"""

import argparse
import json
import signal
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src import db, jobs
from src.actions import (
    ActionAdapter,
    ActionResult,
    action_for_step,
    adapter_from_environment,
)

ACTION_ADAPTER: ActionAdapter = adapter_from_environment()
JOB_ONBOARDING_ITEM = "onboarding.item.execute"


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _add_audit(
    session: Session,
    *,
    job: db.WorkflowJobRecord,
    action: str,
    status: str,
    started: datetime,
    duration_ms: int,
    error: Optional[str] = None,
    request_id: Optional[int] = None,
    item_id: Optional[int] = None,
) -> None:
    session.add(
        db.ActionExecutionRecord(
            request_id=job.resource_id and int(job.resource_id) or 0,
            checklist_item_id=0,
            action=action,
            actor_subject=None,
            adapter=type(ACTION_ADAPTER).__name__,
            correlation_id=_new_run_id(),
            attempt=job.attempts,
            status=status,
            error_category=type(error).__name__ if error else None,
            error_detail=error,
            started_at=started,
            completed_at=db.utcnow(),
            duration_ms=duration_ms,
        )
    )


def _complete_onboarding_job(
    session: Session, job: db.WorkflowJobRecord, payload: dict[str, Any]
) -> dict[str, Any]:
    """Execute one onboarding item and finish the job in the same transaction."""
    started = db.utcnow()
    clock = time.perf_counter()
    request_id = int(payload["request_id"])
    item_id = int(payload["item_id"])
    request = session.get(db.OnboardingRequestRecord, request_id)
    item = session.get(db.ChecklistItemRecord, item_id)
    if request is None or item is None or item.request_id != request.id:
        raise _PermanentJobError("Onboarding request or checklist item no longer exists.")
    if item.status == "completed":
        return {"already_completed": True, "item_id": item.id}

    required_status = "approved" if item.requires_approval else "pending"
    if item.status != required_status:
        raise _PermanentJobError(
            f"Item must be '{required_status}' before execution "
            f"(current status: '{item.status}')."
        )

    action = payload.get("action") or action_for_step(item.step)
    context = {
        "request_id": request.id,
        "request_type": request.request_type,
        "employee_name": request.employee_name,
        "employee_email": request.employee_email,
        "department": request.department,
    }
    adapter_result: ActionResult = ACTION_ADAPTER.execute(action, context)
    item.status = "completed"
    session.flush()

    session.execute(
        select(db.ChecklistItemRecord).where(
            db.ChecklistItemRecord.request_id == request.id
        )
    )
    items = session.execute(
        select(db.ChecklistItemRecord).where(
            db.ChecklistItemRecord.request_id == request.id
        )
    ).scalars().all()
    if all(entry.status == "completed" for entry in items):
        request.status = "completed"

    duration_ms = int((time.perf_counter() - clock) * 1000)
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action=f"onboarding.action.{action}",
        status="success",
        ticket_id=str(request.id),
        duration_ms=duration_ms,
        detail=f"worker job #{job.id}; attempts={job.attempts}; {adapter_result.detail}",
    )
    _add_audit(
        session,
        job=job,
        action=action,
        status="success",
        started=started,
        duration_ms=duration_ms,
        request_id=request.id,
        item_id=item.id,
    )
    finished = jobs.complete_job(
        session,
        job.id,
        job.lease_owner_token or "",
        result={"action": action, "item_id": item.id},
    )
    if not finished:
        session.rollback()
        return {"fenced": True, "item_id": item.id}
    session.commit()
    return {"action": action, "item_id": item.id}


class _PermanentJobError(Exception):
    """A failure that should not be retried (invalid state or missing data)."""


def _execute_job(session: Session, job: db.WorkflowJobRecord) -> str:
    if job.job_type != JOB_ONBOARDING_ITEM:
        jobs.fail_job(
            session,
            job.id,
            job.lease_owner_token or "",
            f"Unsupported job type: {job.job_type}",
            retryable=False,
        )
        session.commit()
        return "failed"

    payload = json.loads(job.payload_json)
    try:
        _complete_onboarding_job(session, job, payload)
        return "succeeded"
    except _PermanentJobError as error:
        jobs.fail_job(
            session,
            job.id,
            job.lease_owner_token or "",
            str(error),
            retryable=False,
        )
        session.commit()
        return "failed"
    except Exception as error:  # adapter failures are retryable
        session.rollback()
        reloaded = session.get(db.WorkflowJobRecord, job.id)
        if reloaded is None:
            return "missing"
        status = jobs.fail_job(
            session,
            reloaded.id,
            reloaded.lease_owner_token or "",
            str(error),
            retryable=True,
        )
        session.commit()
        return status or "fenced"


def run_once(
    *,
    worker_id: Optional[str] = None,
    lease_seconds: int = jobs.DEFAULT_LEASE_SECONDS,
) -> Optional[db.WorkflowJobRecord]:
    """Claim and execute at most one due job."""
    db._ensure_tables()
    session = db.SessionLocal()
    try:
        job = jobs.claim_job(
            session, worker_id, lease_seconds=lease_seconds
        )
        if job is None:
            return None
        _execute_job(session, job)
        session.expire_all()
        return session.get(db.WorkflowJobRecord, job.id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="FlowOps durable workflow worker")
    parser.add_argument("--once", action="store_true", help="process at most one job")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="seconds between empty polls",
    )
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=jobs.DEFAULT_LEASE_SECONDS,
        help="lease duration for claimed jobs",
    )
    parser.add_argument(
        "--worker-id",
        default=None,
        help="stable worker identifier (defaults to a random id)",
    )
    args = parser.parse_args(argv)
    worker_id = args.worker_id or f"worker-{uuid.uuid4().hex[:8]}"
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while not stopping:
        job = run_once(worker_id=worker_id, lease_seconds=args.lease_seconds)
        if job is not None:
            print(f"{job.id} {job.status} {job.job_type}", flush=True)
            if args.once:
                return 0
        elif args.once:
            return 0
        if not args.once:
            time.sleep(max(0.1, args.poll_interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
