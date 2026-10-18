"""Durable workflow worker for onboarding action execution.

Run once (useful in tests):

    python -m src.worker --once

Or as a polling daemon:

    python -m src.worker --poll-interval 2
"""

import argparse
import hashlib
import json
import signal
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src import db, definitions, jobs, scheduler, triggers, workflow
from src.actions import (
    ActionAdapter,
    ActionResult,
    action_for_step,
    adapter_from_environment,
)

ACTION_ADAPTER: ActionAdapter = adapter_from_environment()
JOB_ONBOARDING_ITEM = jobs.JOB_ONBOARDING_ITEM
JOB_TRIGGER_EXECUTE = jobs.JOB_TRIGGER_EXECUTE


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
            request_id=request_id or 0,
            checklist_item_id=item_id or 0,
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


class _LeaseHeartbeat:
    """Extend a job lease while an action is running, stopping on takeover."""

    def __init__(
        self,
        job_id: int,
        owner_token: Optional[str],
        lease_seconds: int,
        interval: Optional[float],
    ) -> None:
        self.job_id = job_id
        self.owner_token = owner_token
        self.lease_seconds = lease_seconds
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "_LeaseHeartbeat":
        if not self.owner_token or not self.interval or self.interval <= 0:
            return self
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            session = db.SessionLocal()
            try:
                still_owned = jobs.heartbeat_job(
                    session,
                    self.job_id,
                    self.owner_token or "",
                    lease_seconds=self.lease_seconds,
                )
                session.commit()
                if not still_owned:
                    self._stop.set()
                    return
            except Exception:
                session.rollback()
                return
            finally:
                session.close()


def _complete_onboarding_job(
    session: Session,
    job: db.WorkflowJobRecord,
    payload: dict[str, Any],
    *,
    lease_seconds: int = jobs.DEFAULT_LEASE_SECONDS,
    heartbeat_interval: Optional[float] = None,
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
        # A previous execution may have completed the checklist item before
        # losing its lease or committing the job row. Finalize the job as an
        # idempotent replay instead of returning with the job stuck in
        # ``running`` forever.
        finished = jobs.complete_job(
            session,
            job.id,
            job.lease_owner_token or "",
            result={"already_completed": True, "item_id": item.id},
        )
        if not finished:
            session.rollback()
            return {"fenced": True, "item_id": item.id}
        scheduler.advance_from_job(
            session, job, success=True, output={"already_completed": True}
        )
        session.commit()
        return {"already_completed": True, "item_id": item.id}

    items = session.execute(
        select(db.ChecklistItemRecord).where(
            db.ChecklistItemRecord.request_id == request.id
        )
    ).scalars().all()
    missing = workflow.missing_prerequisites(item, items)
    if missing:
        raise _PermanentJobError(workflow.blocked_status_detail(item, missing))
    required_status = "approved" if item.requires_approval else "pending"
    if item.status != required_status:
        raise _PermanentJobError(
            f"Item must be '{required_status}' before execution "
            f"(current status: '{item.status}')."
        )

    action = payload.get("action") or action_for_step(item.step)
    idempotency_key = job.idempotency_key or f"onboarding:{request.id}:{item.id}"
    context = {
        "request_id": request.id,
        "request_type": request.request_type,
        "employee_name": request.employee_name,
        "employee_email": request.employee_email,
        "department": request.department,
        "tenant_key": request.tenant_key,
        "idempotency_key": idempotency_key,
        "job_id": job.id,
        **workflow.action_context_outputs(item, items),
    }
    interval = heartbeat_interval
    if interval is None:
        interval = max(0.25, lease_seconds / 3)
    with _LeaseHeartbeat(
        job.id, job.lease_owner_token, lease_seconds, interval
    ):
        adapter_result: ActionResult = ACTION_ADAPTER.execute(action, context)
    item.status = "completed"
    workflow.record_output(item, adapter_result.output)
    session.flush()
    workflow.apply_graph_transitions(
        session, items, request_id=request.id, actor_subject=job.lease_owner_token
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
    scheduler.advance_from_job(
        session, job, success=True, output=adapter_result.output
    )
    session.commit()
    return {"action": action, "item_id": item.id}


class _PermanentJobError(Exception):
    """A failure that should not be retried (invalid state or missing data)."""


def _cascade_terminal_failure(session: Session, payload: dict[str, Any]) -> None:
    """Mark a terminally failed item and cascade its dependents."""
    item_id = payload.get("item_id")
    request_id = payload.get("request_id")
    if item_id is None or request_id is None:
        return
    item = session.get(db.ChecklistItemRecord, int(item_id))
    if item is None or item.status == "completed":
        return
    item.status = "failed"
    items = session.execute(
        select(db.ChecklistItemRecord).where(
            db.ChecklistItemRecord.request_id == int(request_id)
        )
    ).scalars().all()
    workflow.apply_graph_transitions(
        session,
        items,
        request_id=int(request_id),
        actor_subject="workflow-worker",
        reason="terminal job failure",
    )


def _notify_worker_failure(
    session: Session,
    job: db.WorkflowJobRecord,
    payload: dict[str, Any],
    error: str,
) -> None:
    import os
    url = os.environ.get("FLOWOPS_ACTION_FAILURE_WEBHOOK_URL")
    if not url:
        return
    from src.notify import deliver_webhook

    action = payload.get("action") or "unknown"
    delivery = deliver_webhook(
        url,
        {
            "event": "workflow.job.failed",
            "job_id": job.id,
            "job_type": job.job_type,
            "action": action,
            "payload": payload,
            "attempts": job.attempts,
            "error": error,
        },
    )
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action="notification.action_failure",
        status="success" if delivery.success else "failure",
        ticket_id=str(payload.get("request_id") or job.id),
        duration_ms=0,
        detail=f"job #{job.id} attempts={delivery.attempts}; {delivery.last_error or 'delivered'}",
    )


def _complete_trigger_job(
    session: Session,
    job: db.WorkflowJobRecord,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Execute a trigger delivery and create the target resource."""
    delivery_id = payload.get("delivery_id")
    trigger_key = payload.get("trigger_key")
    event_payload = payload.get("event_payload", {})

    delivery = session.get(db.TriggerDeliveryRecord, delivery_id)
    if delivery is None:
        raise _PermanentJobError("Trigger delivery not found.")
    trigger = session.get(db.TriggerRecord, trigger_key)
    if trigger is None:
        raise _PermanentJobError("Trigger not found.")

    try:
        mapped = triggers.apply_mapping(
            json.loads(trigger.mapping_json), event_payload, trigger
        )
    except (triggers.MappingError, json.JSONDecodeError) as error:
        raise _PermanentJobError(f"Mapping failed: {error}") from error

    request_id = None
    error = None

    if trigger.target_type == "ticket":
        # Create ticket using existing intake path
        from src.ticket_processor import normalize_ticket
        from src.routing import route_ticket

        ticket_id = f"trig-{trigger.key}-{delivery.event_id[:8]}"
        normalized = normalize_ticket(
            {
                "userId": mapped.get("requester_id", 1),
                "id": int(hashlib.sha256(ticket_id.encode()).hexdigest()[:9], 16),
                "title": mapped.get("title", f"Trigger: {trigger.name}"),
                "completed": False,
            }
        )
        category, queue = route_ticket(normalized)
        ticket = db.TicketRecord(
            ticket_id=ticket_id,
            requester_id=normalized.userId,
            title=normalized.title,
            status="open",
            priority=normalized.priority,
            category=category,
            queue=queue,
        )
        session.add(ticket)
        request_id = ticket_id

    elif trigger.target_type in ("onboarding_request", "offboarding_request"):
        # Create onboarding/offboarding request pinned to definition
        if not trigger.definition_key:
            raise _PermanentJobError("Definition key is required for onboarding/offboarding triggers.")
        definition, version = definitions.published_version(session, trigger.definition_key)
        if definition.request_type != trigger.target_type.replace("_request", ""):
            raise _PermanentJobError("Definition request type mismatch.")

        record = db.OnboardingRequestRecord(
            request_type=definition.request_type,
            employee_name=mapped.get("employee_name", "Trigger Employee"),
            employee_email=mapped.get("employee_email", "trigger@example.com"),
            department=mapped.get("department", "IT"),
            requester_id=mapped.get("requester_id", 1),
            start_date=mapped.get("start_date"),
            definition_key=definition.key,
            definition_version=version.version,
        )
        session.add(record)
        session.flush()

        steps = json.loads(version.steps_json)
        for template in steps:
            from src.onboarding import initial_status_for
            session.add(
                db.ChecklistItemRecord(
                    request_id=record.id,
                    step_key=template["key"],
                    step=template["step"],
                    queue=template["queue"],
                    requires_approval=template["requires_approval"],
                    action=template.get("action"),
                    status=initial_status_for(template),
                    depends_on_json=json.dumps(template.get("depends_on", [])),
                    join_mode=template.get("join", "all"),
                    when_json=json.dumps(template.get("when", [])),
                    sla_minutes=template.get("sla_minutes"),
                    escalation_queue=template.get("escalation_queue"),
                )
            )
        session.flush()
        from src import timers
        timers.sync_timers(session, record.id)
        request_id = str(record.id)

    else:
        raise _PermanentJobError(f"Unsupported target type: {trigger.target_type}")

    # Update delivery
    triggers.update_delivery(
        session,
        delivery,
        status="succeeded",
        request_id=request_id,
    )

    # Log audit
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action="trigger.delivery.succeeded",
        status="success",
        ticket_id=request_id,
        duration_ms=0,
        detail=f"trigger={trigger.key}, event_id={delivery.event_id}",
        actor_subject=f"trigger:{trigger.key}",
    )

    return {"request_id": request_id}


def _execute_job(
    session: Session,
    job: db.WorkflowJobRecord,
    *,
    lease_seconds: int = jobs.DEFAULT_LEASE_SECONDS,
    heartbeat_interval: Optional[float] = None,
) -> str:
    if job.job_type == JOB_TRIGGER_EXECUTE:
        payload = json.loads(job.payload_json)
        try:
            _complete_trigger_job(session, job, payload)
            jobs.complete_job(
                session,
                job.id,
                job.lease_owner_token or "",
                result={"delivery_id": payload.get("delivery_id")},
            )
            session.commit()
            return "succeeded"
        except _PermanentJobError as error:
            session.rollback()
            reloaded = session.get(db.WorkflowJobRecord, job.id)
            if reloaded is None:
                return "missing"
            status = jobs.fail_job(
                session,
                reloaded.id,
                reloaded.lease_owner_token or "",
                str(error),
                retryable=False,
            )
            session.commit()
            return status or "fenced"
        except Exception as error:
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
        _complete_onboarding_job(
            session,
            job,
            payload,
            lease_seconds=lease_seconds,
            heartbeat_interval=heartbeat_interval,
        )
        return "succeeded"
    except _PermanentJobError as error:
        jobs.fail_job(
            session,
            job.id,
            job.lease_owner_token or "",
            str(error),
            retryable=False,
        )
        _notify_worker_failure(session, job, payload, str(error))
        session.commit()
        return "failed"
    except Exception as error:
        # Retryable unless the adapter says otherwise (Graph 4xx, licence
        # exhausted, invalid PowerShell input): those go straight to failed.
        session.rollback()
        reloaded = session.get(db.WorkflowJobRecord, job.id)
        if reloaded is None:
            return "missing"
        status = jobs.fail_job(
            session,
            reloaded.id,
            reloaded.lease_owner_token or "",
            str(error),
            retryable=getattr(error, "retryable", True) is not False,
        )
        if status == "failed":
            _cascade_terminal_failure(session, payload)
            scheduler.advance_from_job(
                session, reloaded, success=False, error=str(error)
            )
            _notify_worker_failure(session, reloaded, payload, str(error))
        session.commit()
        return status or "fenced"


def run_once(
    *,
    worker_id: Optional[str] = None,
    lease_seconds: int = jobs.DEFAULT_LEASE_SECONDS,
    heartbeat_interval: Optional[float] = None,
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
        _execute_job(
            session,
            job,
            lease_seconds=lease_seconds,
            heartbeat_interval=heartbeat_interval,
        )
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
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=None,
        help="seconds between lease heartbeats (defaults to lease/3)",
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
        job = run_once(
            worker_id=worker_id,
            lease_seconds=args.lease_seconds,
            heartbeat_interval=args.heartbeat_interval,
        )
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
