"""Onboarding/offboarding workflow service layer.

Owns the request/checklist loaders and response shapers, the durable item
transitions (approve/complete/execute), the run-control idempotency
protocol (pause/resume/cancel), and the run-scoped step decisions. The
routers in ``src/routes/onboarding.py`` stay thin: they authenticate,
delegate, and serialize.

``ACTION_ADAPTER`` lives here (beside its only consumer, ``_execute_item``);
tests monkeypatch it at ``src.services.onboarding.ACTION_ADAPTER``.

Shared api.py helpers (idempotency protocol, action configuration) are
imported via the established ``src.api`` late-import pattern: api.py
defines those names before registering routers.
"""

import hashlib
import json
import time
from collections import defaultdict
from datetime import timezone
from typing import Literal

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from src import db, definitions, idempotency, scheduler, timers, workflow
from src.actions import ActionAdapter, ActionError, action_for_step, adapter_from_environment
from src.api import (  # noqa: E402
    _action_attempts,
    _complete_idempotency_key,
    _notify_action_failure,
    _release_idempotency_key,
    _reserve_idempotency_key,
    _validate_idempotency_key,
)
from src.auth import Actor
from src.deps import (
    _actor_tenant,
    _new_run_id,
    _require_request_access,
    _require_workflow_run_access,
)
from src.onboarding import checklist_for, initial_status_for
from src.schemas import (
    ChecklistItemOut,
    OnboardingRequestIn,
    OnboardingRequestOut,
    WorkflowEventOut,
    WorkflowRunOut,
    WorkflowStepRunOut,
)


def _create_onboarding_request(
    request: OnboardingRequestIn,
    session: Session,
    actor_subject: str | None = None,
) -> OnboardingRequestOut:
    """Create the record and checklist, returning the API response shape."""
    started = time.perf_counter()
    if request.tenant_key:
        tenant = session.get(db.TenantRecord, request.tenant_key)
        if tenant is None or not tenant.active:
            raise HTTPException(422, f"Unknown or inactive tenant: {request.tenant_key}")
    record = db.OnboardingRequestRecord(
        request_type=request.request_type,
        employee_name=request.employee_name,
        employee_email=request.employee_email,
        department=request.department,
        requester_id=request.requester_id,
        start_date=request.start_date,
        tenant_key=request.tenant_key,
    )
    session.add(record)
    session.flush()
    if request.definition_key:
        definition, version = definitions.published_version(
            session, request.definition_key
        )
        if definition.request_type != request.request_type:
            raise HTTPException(422, "Definition request_type does not match request.")
        if request.definition_version is not None and request.definition_version != version.version:
            selected = definitions.get_version(
                session, request.definition_key, request.definition_version
            )
            if selected is None:
                raise HTTPException(422, "Definition version not found.")
            version = selected
        steps = json.loads(version.steps_json)
        record.definition_key = definition.key
        record.definition_version = version.version
    else:
        steps = checklist_for(request.request_type, template=request.template)
    for template in steps:
        # The template's ``key`` becomes the row's ``step_key`` column;
        # depends_on is persisted as JSON for the graph resolver.
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
    timers.sync_timers(session, record.id)
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action="onboarding.request.created",
        status="success",
        duration_ms=int((time.perf_counter() - started) * 1000),
        detail=f"{request.request_type} for {request.employee_name}: "
               f"{len(steps)} checklist steps generated",
        actor_subject=actor_subject,
    )
    _, items = _load_request(session, record.id)
    return _request_out(record, items)


# v0.6 action boundary: the adapter that executes checklist actions. Tests
# monkeypatch this module attribute with failing/succeeding doubles.
ACTION_ADAPTER: ActionAdapter = adapter_from_environment()

def _request_out(
    record: db.OnboardingRequestRecord, items: list[db.ChecklistItemRecord]
) -> OnboardingRequestOut:
    return OnboardingRequestOut(
        **{c: getattr(record, c) for c in (
            "id", "request_type", "employee_name", "employee_email",
            "department", "requester_id", "start_date", "status",
            "definition_key", "definition_version", "tenant_key",
        )},
        checklist=[
            ChecklistItemOut(
                **{c: getattr(item, c) for c in (
                    "id", "step", "step_key", "queue", "requires_approval",
                    "status", "action",
                )},
                result=json.loads(item.result) if item.result else None,
                depends_on=json.loads(item.depends_on_json)
                if item.depends_on_json
                else [],
                output=json.loads(item.output_json)
                if item.output_json
                else None,
                join_mode=item.join_mode or "all",
                when=json.loads(item.when_json) if item.when_json else [],
                sla_minutes=item.sla_minutes,
                overdue=bool(getattr(item, "overdue", False)),
                sla_remaining_minutes=getattr(item, "sla_remaining_minutes", None),
                sla_due_at=getattr(item, "sla_due_at", None),
            )
            for item in sorted(items, key=lambda i: i.id)
        ],
    )


def _load_checklists(
    session: Session, request_ids: list[int],
) -> dict[int, list[db.ChecklistItemRecord]]:
    grouped: dict[int, list[db.ChecklistItemRecord]] = defaultdict(list)
    if not request_ids:
        return grouped
    now = db.utcnow()
    # Keep parameter counts bounded for existing unpaginated callers.
    for offset in range(0, len(request_ids), 500):
        batch = request_ids[offset:offset + 500]
        items = session.execute(
            select(db.ChecklistItemRecord).where(db.ChecklistItemRecord.request_id.in_(batch))
        ).scalars().all()
        timers_by_item = {
            row.item_id: row for row in session.execute(
                select(db.WorkflowTimerRecord).where(
                    db.WorkflowTimerRecord.request_id.in_(batch),
                    db.WorkflowTimerRecord.status == "pending",
                ).order_by(db.WorkflowTimerRecord.due_at.desc())
            ).scalars().all()
        }
        for item in items:
            timer = timers_by_item.get(item.id)
            due = timer.due_at if timer else None
            if due is not None and due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            item.sla_due_at = due
            item.overdue = due is not None and due <= now
            item.sla_remaining_minutes = max(0, int((due - now).total_seconds() // 60)) if due is not None else None
            grouped[item.request_id].append(item)
    return grouped


def _load_request(
    session: Session, request_id: int
) -> tuple[db.OnboardingRequestRecord, list[db.ChecklistItemRecord]]:
    record = session.get(db.OnboardingRequestRecord, request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Onboarding request not found.")
    return record, _load_checklists(session, [request_id]).get(request_id, [])

def _workflow_run_out(
    session: Session, run: db.WorkflowRunRecord, *,
    loaded: tuple | None = None,
) -> WorkflowRunOut:
    if loaded is None:
        request, items = _load_request(session, run.request_id)
        steps = session.execute(
            select(db.WorkflowStepRunRecord)
            .where(db.WorkflowStepRunRecord.run_id == run.id)
            .order_by(db.WorkflowStepRunRecord.id)
        ).scalars().all()
        events = session.execute(
            select(db.WorkflowEventRecord)
            .where(db.WorkflowEventRecord.run_id == run.id)
            .order_by(db.WorkflowEventRecord.id)
        ).scalars().all()
    else:
        request, items, steps, events = loaded
    return WorkflowRunOut(
        id=run.id,
        request_id=run.request_id,
        status=run.status,
        max_parallel=run.max_parallel,
        error=run.error,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        checklist=_request_out(request, items).checklist,
        definition_key=request.definition_key,
        employee_name=request.employee_name,
        steps=[
            WorkflowStepRunOut(
                id=row.id,
                checklist_item_id=row.checklist_item_id,
                attempt=row.attempt,
                status=row.status,
                job_id=row.job_id,
                scheduled_at=row.scheduled_at,
                started_at=row.started_at,
                completed_at=row.completed_at,
                output=json.loads(row.output_json) if row.output_json else None,
                error=row.error,
            )
            for row in steps
        ],
        events=[
            WorkflowEventOut(
                id=event.id,
                event_type=event.event_type,
                step_run_id=event.step_run_id,
                payload=json.loads(event.payload_json) if event.payload_json else None,
                created_at=event.created_at,
            )
            for event in events
        ],
    )


def _run_action(
    session: Session, run_id: int, actor: Actor, action: str,
    response: Response, idempotency_key: str | None,
) -> WorkflowRunOut:
    run = session.get(db.WorkflowRunRecord, run_id)
    if run is None:
        raise HTTPException(404, "Workflow run not found.")
    _require_workflow_run_access(session, actor, run)
    _validate_idempotency_key(idempotency_key)
    scope = idempotency.SCOPE_RUN_CONTROL
    key = ("run-control:" + hashlib.sha256(json.dumps(
        [_actor_tenant(actor), actor.subject, idempotency_key], separators=(",", ":")
    ).encode()).hexdigest()) if idempotency_key else None
    fingerprint = hashlib.sha256(json.dumps([run_id, action]).encode()).hexdigest()
    owner, replay = _reserve_idempotency_key(
        session, response, key, fingerprint, scope, str(run.request_id),
        actor_subject=actor.subject,
    )
    if replay is not None:
        return WorkflowRunOut.model_validate(replay["workflow_run"])
    try:
        run = scheduler._locked_run(session, run_id)
        if run is None:
            raise HTTPException(404, "Workflow run not found.")
        _require_workflow_run_access(session, actor, run)
        allowed = {"pause": {"running", "paused"}, "resume": {"paused", "running"},
                   "cancel": {"pending", "running", "paused", "cancelled"}}
        if run.status not in allowed[action]:
            raise HTTPException(409, f"Cannot {action} a run with status '{run.status}'.")
        if action == "pause":
            run = scheduler.pause_run(session, run_id, actor.subject)
        elif action == "resume":
            run = scheduler.resume_run(session, run_id, actor.subject)
        else:
            run = scheduler.cancel_run(session, run_id, actor.subject)
        session.flush()
        result = _workflow_run_out(session, run)
        if key and owner:
            if not _complete_idempotency_key(
                session, key, owner, fingerprint, scope, str(result.request_id),
                {"workflow_run": result.model_dump(mode="json")},
            ):
                raise HTTPException(409, "Run-control reservation changed; retry.")
        else:
            session.commit()
        return result
    except Exception as error:
        session.rollback()
        if key and owner:
            _release_idempotency_key(session, key, owner)
        if isinstance(error, LookupError):
            raise HTTPException(404, str(error)) from error
        raise


class ApprovalDecisionIn(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


def _decide_workflow_step(
    session: Session,
    run_id: int,
    item_id: int,
    action: Literal["approve", "reject"],
    payload: ApprovalDecisionIn,
    actor: Actor,
) -> WorkflowRunOut:
    reason = payload.reason.strip()
    if len(reason) < 3:
        raise HTTPException(422, "A decision reason is required.")
    run = session.execute(
        select(db.WorkflowRunRecord).where(db.WorkflowRunRecord.id == run_id).with_for_update()
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "Workflow run not found.")
    _require_workflow_run_access(session, actor, run)
    if run.status not in ("pending", "running"):
        raise HTTPException(409, "Workflow run is not active.")
    target = "approved" if action == "approve" else "rejected"
    item = session.get(db.ChecklistItemRecord, item_id)
    if item is None or item.request_id != run.request_id:
        raise HTTPException(404, "Checklist item not found.")
    if not item.requires_approval or item.status != "pending":
        raise HTTPException(409, "Checklist item is not awaiting approval.")
    changed = session.execute(
        update(db.ChecklistItemRecord).where(
            db.ChecklistItemRecord.id == item_id,
            db.ChecklistItemRecord.request_id == run.request_id,
            db.ChecklistItemRecord.requires_approval.is_(True),
            db.ChecklistItemRecord.status == "pending",
        ).values(status=target)
    )
    if changed.rowcount != 1:
        raise HTTPException(409, "Checklist item state changed; refresh and retry.")
    session.refresh(item)
    _, items = _load_request(session, run.request_id)
    workflow.apply_graph_transitions(
        session, items, request_id=run.request_id, actor_subject=actor.subject, reason=reason
    )
    timers.sync_timers(session, run.request_id)
    db.log_execution(
        session, run_id=_new_run_id(), action=f"onboarding.item.{target}",
        status="success", ticket_id=str(run.request_id), duration_ms=0,
        detail=f"step '{item.step}'; reason={reason}", actor_subject=actor.subject,
    )
    scheduler.tick(session, run.id, worker_id="console-transition", commit=False)
    session.flush()
    return _workflow_run_out(session, run)


def _get_item(
    session: Session, record: db.OnboardingRequestRecord, item_id: int
) -> db.ChecklistItemRecord:
    item = session.get(db.ChecklistItemRecord, item_id)
    if item is None or item.request_id != record.id:
        raise HTTPException(status_code=404, detail="Checklist item not found.")
    return item


def _transition_item(
    session: Session,
    request_id: int,
    item_id: int,
    request: Request,
    action: Literal["approve", "complete"],
    actor_subject: str | None = None,
) -> OnboardingRequestOut:
    record, items = _load_request(session, request_id)
    item = _get_item(session, record, item_id)

    if action == "approve":
        if not item.requires_approval:
            raise HTTPException(409, "This checklist item does not require approval.")
        if item.status != "pending":
            raise HTTPException(409, f"Cannot approve an item with status '{item.status}'.")
        item.status = "approved"
    else:
        allowed_from = "approved" if item.requires_approval else "pending"
        missing = workflow.missing_prerequisites(item, items)
        if missing:
            raise HTTPException(
                409, workflow.blocked_status_detail(item, missing)
            )
        if item.status != allowed_from:
            raise HTTPException(
                409,
                f"Item must be '{allowed_from}' before completion "
                f"(current status: '{item.status}').",
            )
        item.status = "completed"
    session.flush()

    if action == "complete":
        workflow.apply_graph_transitions(
            session, items, request_id=record.id, actor_subject=actor_subject
        )
        timers.sync_timers(session, record.id)

    db.log_execution(
        session,
        run_id=_new_run_id(),
        action=f"onboarding.item.{action}d",
        status="success",
        duration_ms=0,
        detail=f"step '{item.step}' of request #{record.id} is now '{item.status}'",
        actor_subject=actor_subject,
    )

    _, items = _load_request(session, record.id)
    if all(i.status == "completed" for i in items) and record.status != "completed":
        record.status = "completed"
        db.log_execution(
            session,
            run_id=_new_run_id(),
            action="onboarding.request.completed",
            status="success",
            duration_ms=0,
            detail=f"all checklist steps done for request #{record.id}",
            actor_subject=actor_subject,
        )
    _, items = _load_request(session, record.id)
    run = scheduler.get_run_for_request(session, record.id)
    if run is not None:
        scheduler.tick(session, run.id, worker_id="manual-transition", commit=False)
    return _request_out(record, items)


def _execute_item(
    session: Session, request_id: int, item_id: int, actor: Actor
) -> OnboardingRequestOut:
    """Run one approved checklist action with bounded retries."""
    record, items = _load_request(session, request_id)
    _require_request_access(actor, record)
    item = _get_item(session, record, item_id)
    missing = workflow.missing_prerequisites(item, items)
    if missing:
        raise HTTPException(409, workflow.blocked_status_detail(item, missing))
    required_status = "approved" if item.requires_approval else "pending"
    if item.status != required_status:
        raise HTTPException(
            409,
            f"Item must be '{required_status}' before execution "
            f"(current status: '{item.status}').",
        )

    action = action_for_step(item.step)
    correlation_id = _new_run_id()
    context = {
        "request_id": record.id,
        "request_type": record.request_type,
        "employee_name": record.employee_name,
        "employee_email": record.employee_email,
        "department": record.department,
        "actor_subject": actor.subject,
        "actor_roles": list(actor.roles),
        "tenant_key": record.tenant_key,
        "idempotency_key": f"onboarding-action:{record.id}:{item.id}",
        "correlation_id": correlation_id,
        **workflow.action_context_outputs(item, items),
    }
    started_at = db.utcnow()
    started = time.perf_counter()
    last_error = ""
    attempts_limit = _action_attempts(action)
    for attempt in range(1, attempts_limit + 1):
        try:
            result = ACTION_ADAPTER.execute(action, context)
            completed_at = db.utcnow()
            session.add(db.ActionExecutionRecord(
                request_id=record.id,
                checklist_item_id=item.id,
                action=action,
                actor_subject=actor.subject,
                adapter=getattr(ACTION_ADAPTER, "adapter_name", ACTION_ADAPTER.__class__.__name__),
                correlation_id=correlation_id,
                attempt=attempt,
                status="succeeded",
                external_reference=(result.external_reference or result.detail)[:256],
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=int((time.perf_counter() - started) * 1000),
            ))
            item.status = "completed"
            workflow.record_output(item, result.output)
            session.flush()
            workflow.apply_graph_transitions(
                session, items, request_id=record.id, actor_subject=actor.subject
            )
            db.log_execution(
                session,
                run_id=_new_run_id(),
                action=f"onboarding.action.{action}",
                status="success",
                duration_ms=int((time.perf_counter() - started) * 1000),
                detail=f"attempts={attempt}; {result.detail}",
                actor_subject=actor.subject,
            )
            _, items = _load_request(session, record.id)
            if all(i.status == "completed" for i in items):
                record.status = "completed"
                db.log_execution(
                    session,
                    run_id=_new_run_id(),
                    action="onboarding.request.completed",
                    status="success",
                    duration_ms=0,
                    detail=f"all checklist steps done for request #{record.id}",
                )
            _, items = _load_request(session, record.id)
            return _request_out(record, items)
        except Exception as error:  # adapter failures must be logged and classified
            last_error = str(error)
            retryable = not isinstance(error, ActionError) or error.retryable
            session.add(db.ActionExecutionRecord(
                request_id=record.id,
                checklist_item_id=item.id,
                action=action,
                actor_subject=actor.subject,
                adapter=ACTION_ADAPTER.__class__.__name__,
                correlation_id=correlation_id,
                attempt=attempt,
                status="retrying" if retryable and attempt < attempts_limit else "failed",
                error_category=getattr(error, "category", "adapter_error"),
                error_detail=last_error[:1000],
                started_at=started_at,
                completed_at=db.utcnow(),
                duration_ms=int((time.perf_counter() - started) * 1000),
            ))
            if not retryable:
                attempts_limit = attempt
                break

    item.status = "failed"
    workflow.apply_graph_transitions(
        session, items, request_id=record.id, actor_subject=actor.subject
    )
    _notify_action_failure(action, context, attempts_limit, last_error, session)
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action=f"onboarding.action.{action}",
        status="failure",
        duration_ms=int((time.perf_counter() - started) * 1000),
        detail=f"attempts={attempts_limit}; {last_error}",
        actor_subject=actor.subject,
    )
    session.flush()
    session.commit()
    raise HTTPException(
        502,
        f"Action '{action}' failed after {attempts_limit} attempts.",
    )
