"""DB-backed workflow orchestrator for v1.2.

The scheduler leases one run at a time, resolves the DAG, and enqueues ready
steps through the existing durable jobs table. It never auto-approves gated
steps; operator/ChatOps approval remains the only way to make them executable.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from src import db, jobs, workflow

RUN_STATUSES = ("pending", "running", "paused", "cancelled", "completed", "failed")
ACTIVE_STEP_STATUSES = ("scheduled", "running")
TERMINAL_RUN_STATUSES = ("cancelled", "completed", "failed")
DEFAULT_MAX_PARALLEL = 4
DEFAULT_LEASE_SECONDS = 60
TICK_COUNTS = {"claimed": 0, "empty": 0}


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def max_parallel_default() -> int:
    return _env_int("FLOWOPS_DAG_MAX_PARALLEL", DEFAULT_MAX_PARALLEL)


def lease_seconds_default() -> int:
    return _env_int("FLOWOPS_DAG_LEASE_SECONDS", DEFAULT_LEASE_SECONDS)


def _now(value: Optional[datetime] = None) -> datetime:
    return value or db.utcnow()


def _events(session: Session, run_id: int, event_type: str, payload: dict[str, Any], step_run_id: Optional[int] = None) -> None:
    session.add(
        db.WorkflowEventRecord(
            run_id=run_id,
            step_run_id=step_run_id,
            event_type=event_type,
            payload_json=json.dumps(payload, separators=(",", ":"), default=str),
        )
    )


def _log(session: Session, action: str, request_id: int, detail: str, actor: str) -> None:
    db.log_execution(
        session,
        run_id=uuid.uuid4().hex[:12],
        action=action,
        status="success",
        ticket_id=str(request_id),
        duration_ms=0,
        detail=detail,
        actor_subject=actor,
    )


def get_run_for_request(session: Session, request_id: int) -> Optional[db.WorkflowRunRecord]:
    return session.execute(
        select(db.WorkflowRunRecord).where(db.WorkflowRunRecord.request_id == request_id)
    ).scalar_one_or_none()


def get_run(session: Session, run_id: int) -> Optional[db.WorkflowRunRecord]:
    return session.get(db.WorkflowRunRecord, run_id)


def start_run(
    session: Session,
    request_id: int,
    *,
    actor_subject: str = "scheduler",
    max_parallel: Optional[int] = None,
    now: Optional[datetime] = None,
) -> db.WorkflowRunRecord:
    """Start one run per request, idempotently."""
    existing = get_run_for_request(session, request_id)
    if existing is not None:
        return existing
    request = session.get(db.OnboardingRequestRecord, request_id)
    if request is None:
        raise LookupError("Onboarding request not found.")
    run = db.WorkflowRunRecord(
        request_id=request_id,
        status="running",
        max_parallel=max_parallel or max_parallel_default(),
        started_at=_now(now),
    )
    session.add(run)
    session.flush()
    _events(session, run.id, "workflow.run.started", {"request_id": request_id})
    _log(session, "workflow.run.started", request_id, f"run #{run.id} started", actor_subject)
    tick(session, run.id, now=now, worker_id=f"start:{actor_subject}", commit=False)
    return run


def claim_run(
    session: Session,
    *,
    run_id: Optional[int] = None,
    worker_id: str = "scheduler",
    now: Optional[datetime] = None,
    lease_seconds: Optional[int] = None,
) -> Optional[db.WorkflowRunRecord]:
    """Atomically claim a due run lease; returns None if another tick owns it."""
    at = _now(now)
    token = f"{worker_id}:{uuid.uuid4().hex[:16]}"
    table = db.WorkflowRunRecord
    claimable = and_(
        table.status == "running",
        or_(table.lease_owner_token.is_(None), table.lease_expires_at.is_(None), table.lease_expires_at < at),
    )
    if run_id is not None:
        candidate_ids = [run_id]
    else:
        candidate_ids = session.execute(
            select(table.id).where(claimable).order_by(table.id).limit(10)
        ).scalars().all()
    for candidate in candidate_ids:
        result = session.execute(
            update(table)
            .execution_options(synchronize_session=False)
            .where(and_(table.id == candidate, claimable))
            .values(
                lease_owner_token=token,
                lease_expires_at=at + timedelta(seconds=lease_seconds or lease_seconds_default()),
                last_tick_at=at,
            )
        )
        if result.rowcount == 1:
            session.flush()
            session.expire_all()
            return session.get(table, candidate)
    return None


def _active_step_runs(step_runs: Iterable[db.WorkflowStepRunRecord]) -> list[db.WorkflowStepRunRecord]:
    return [row for row in step_runs if row.status in ACTIVE_STEP_STATUSES]


def plan_ready_steps(
    run: db.WorkflowRunRecord,
    items: Sequence[db.ChecklistItemRecord],
    step_runs: Sequence[db.WorkflowStepRunRecord],
    *,
    max_parallel: Optional[int] = None,
) -> list[db.ChecklistItemRecord]:
    """Pure planning: pick ready, executable, not-yet-active steps."""
    if run.status != "running":
        return []
    limit = max_parallel or run.max_parallel or max_parallel_default()
    active = _active_step_runs(step_runs)
    active_item_ids = {row.checklist_item_id for row in active}
    slots = max(0, limit - len(active))
    ready: list[db.ChecklistItemRecord] = []
    for item in sorted(items, key=lambda row: row.id):
        if slots <= 0:
            break
        if item.id in active_item_ids:
            continue
        if item.status == "approved":
            executable = True
        elif item.status == "pending" and not item.requires_approval:
            executable = True
        else:
            executable = False
        if not executable:
            continue
        if not workflow.is_ready(item, items):
            continue
        ready.append(item)
        slots -= 1
    return ready


def _finish_if_done(session: Session, run: db.WorkflowRunRecord, items: Sequence[db.ChecklistItemRecord], step_runs: Sequence[db.WorkflowStepRunRecord], now: datetime) -> None:
    if run.status != "running":
        return
    done = {row.status for row in items}
    if all(item.status in ("completed", "skipped") for item in items):
        run.status = "completed"
        run.completed_at = now
        run.lease_owner_token = None
        run.lease_expires_at = None
        _events(session, run.id, "workflow.run.completed", {})
        _log(session, "workflow.run.completed", run.request_id, f"run #{run.id} completed", "scheduler")
        return
    active = _active_step_runs(step_runs)
    if not active and any(status in ("failed", "rejected", "cascade_blocked", "cancelled") for status in done):
        run.status = "failed"
        run.completed_at = now
        run.error = "one or more steps failed or were blocked"
        run.lease_owner_token = None
        run.lease_expires_at = None
        _events(session, run.id, "workflow.run.failed", {"error": run.error})
        _log(session, "workflow.run.failed", run.request_id, f"run #{run.id} failed", "scheduler")


def tick(
    session: Session,
    run_id: Optional[int] = None,
    *,
    now: Optional[datetime] = None,
    worker_id: str = "scheduler",
    lease_seconds: Optional[int] = None,
    commit: bool = True,
) -> Optional[db.WorkflowRunRecord]:
    """Claim one run and enqueue every currently ready step."""
    at = _now(now)
    run = claim_run(
        session,
        run_id=run_id,
        worker_id=worker_id,
        now=at,
        lease_seconds=lease_seconds,
    )
    if run is None:
        TICK_COUNTS["empty"] += 1
        if commit:
            session.rollback()
        return None
    TICK_COUNTS["claimed"] += 1
    request = session.get(db.OnboardingRequestRecord, run.request_id)
    items = session.execute(
        select(db.ChecklistItemRecord).where(
            db.ChecklistItemRecord.request_id == run.request_id
        )
    ).scalars().all()
    if request is None or not items:
        run.status = "failed"
        run.error = "request or checklist missing"
        run.lease_owner_token = None
        run.lease_expires_at = None
        if commit:
            session.commit()
        return run

    workflow.apply_graph_transitions(
        session, items, request_id=run.request_id, actor_subject="scheduler"
    )
    session.flush()
    step_runs = session.execute(
        select(db.WorkflowStepRunRecord).where(db.WorkflowStepRunRecord.run_id == run.id)
    ).scalars().all()
    _finish_if_done(session, run, items, step_runs, at)

    if run.status == "running":
        ready = plan_ready_steps(run, items, step_runs)
        for item in ready:
            run_step = session.execute(
                select(db.WorkflowStepRunRecord).where(
                    db.WorkflowStepRunRecord.run_id == run.id,
                    db.WorkflowStepRunRecord.checklist_item_id == item.id,
                )
            ).scalars().all()
            attempt = max((row.attempt for row in run_step), default=0) + 1
            key = f"dag:{run.id}:{item.id}:{attempt}"
            action = item.action or "none"
            step_run = db.WorkflowStepRunRecord(
                run_id=run.id,
                checklist_item_id=item.id,
                attempt=attempt,
                status="scheduled",
                scheduled_at=at,
            )
            session.add(step_run)
            session.flush()
            job = jobs.enqueue_job(
                session,
                job_type=jobs.JOB_ONBOARDING_ITEM,
                payload={
                    "run_id": run.id,
                    "step_run_id": step_run.id,
                    "request_id": run.request_id,
                    "item_id": item.id,
                    "action": action,
                },
                idempotency_key=key,
                resource_type="onboarding",
                resource_id=str(run.request_id),
                label=f"{request.employee_name}: {item.step}",
            )
            step_run.job_id = job.id
            _events(
                session,
                run.id,
                "workflow.step.scheduled",
                {"item_id": item.id, "step_run_id": step_run.id, "job_id": job.id, "attempt": attempt},
                step_run.id,
            )
            _log(
                session,
                "workflow.step.scheduled",
                run.request_id,
                f"step '{item.step}' scheduled as job #{job.id} (attempt {attempt})",
                "scheduler",
            )
        run.last_tick_at = at

    run.lease_owner_token = None
    run.lease_expires_at = None
    if commit:
        session.commit()
        session.expire_all()
        return session.get(db.WorkflowRunRecord, run.id)
    session.flush()
    return run


def advance_from_job(
    session: Session,
    job: db.WorkflowJobRecord,
    *,
    success: bool,
    output: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[db.WorkflowRunRecord]:
    """Update a scheduled step ledger and advance its run after a job result."""
    try:
        payload = json.loads(job.payload_json)
    except (TypeError, ValueError):
        return None
    run_id = payload.get("run_id")
    step_run_id = payload.get("step_run_id")
    if run_id is None or step_run_id is None:
        return None
    run = session.get(db.WorkflowRunRecord, int(run_id))
    step_run = session.get(db.WorkflowStepRunRecord, int(step_run_id))
    if run is None or step_run is None:
        return None
    at = _now(now)
    step_run.status = "succeeded" if success else "failed"
    step_run.completed_at = at
    step_run.started_at = step_run.started_at or at
    if success:
        step_run.output_json = json.dumps(output or {}, separators=(",", ":"), default=str)
        step_run.error = None
    else:
        step_run.error = (error or "step failed")[:2000]
    _events(
        session,
        run.id,
        "workflow.step.succeeded" if success else "workflow.step.failed",
        {"item_id": payload.get("item_id"), "step_run_id": step_run.id},
        step_run.id,
    )
    _log(
        session,
        "workflow.step.succeeded" if success else "workflow.step.failed",
        run.request_id,
        f"step run #{step_run.id} {'succeeded' if success else 'failed'}",
        "scheduler",
    )
    session.flush()
    return tick(session, run.id, now=at, worker_id="event", commit=False)


def pause_run(session: Session, run_id: int, actor_subject: str, now: Optional[datetime] = None) -> db.WorkflowRunRecord:
    run = get_run(session, run_id)
    if run is None:
        raise LookupError("Workflow run not found.")
    if run.status == "running":
        run.status = "paused"
        _events(session, run.id, "workflow.run.paused", {})
        _log(session, "workflow.run.paused", run.request_id, f"run #{run.id} paused", actor_subject)
    return run


def resume_run(session: Session, run_id: int, actor_subject: str, now: Optional[datetime] = None) -> db.WorkflowRunRecord:
    run = get_run(session, run_id)
    if run is None:
        raise LookupError("Workflow run not found.")
    if run.status == "paused":
        run.status = "running"
        _events(session, run.id, "workflow.run.resumed", {})
        _log(session, "workflow.run.resumed", run.request_id, f"run #{run.id} resumed", actor_subject)
        session.flush()
        tick(session, run.id, now=now, worker_id="resume", commit=False)
    return run


def cancel_run(session: Session, run_id: int, actor_subject: str, now: Optional[datetime] = None) -> db.WorkflowRunRecord:
    run = get_run(session, run_id)
    if run is None:
        raise LookupError("Workflow run not found.")
    if run.status in ("completed", "cancelled"):
        return run
    run.status = "cancelled"
    run.completed_at = _now(now)
    step_runs = session.execute(
        select(db.WorkflowStepRunRecord).where(db.WorkflowStepRunRecord.run_id == run.id)
    ).scalars().all()
    for step_run in step_runs:
        if step_run.status in ACTIVE_STEP_STATUSES:
            step_run.status = "cancelled"
            step_run.completed_at = run.completed_at
            if step_run.job_id:
                job = session.get(db.WorkflowJobRecord, step_run.job_id)
                if job is not None and job.status in ("queued", "retry_scheduled", "running"):
                    jobs.cancel_job(session, job.id)
    _events(session, run.id, "workflow.run.cancelled", {})
    _log(session, "workflow.run.cancelled", run.request_id, f"run #{run.id} cancelled", actor_subject)
    return run


def tick_all(*, now: Optional[datetime] = None, worker_id: str = "scheduler") -> list[db.WorkflowRunRecord]:
    session = db.SessionLocal()
    try:
        claimed: list[db.WorkflowRunRecord] = []
        while True:
            run = tick(session, now=now, worker_id=worker_id)
            if run is None:
                break
            claimed.append(run)
        return claimed
    finally:
        session.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll", action="store_true")
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args(argv)
    db._ensure_tables()
    if args.once or not args.poll:
        for run in tick_all():
            print(f"advanced run {run.id} ({run.status})")
        return 0
    while True:
        tick_all()
        time.sleep(max(0.1, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
