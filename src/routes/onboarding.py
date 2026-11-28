"""Onboarding/offboarding workflow routes (v0.5-v1.3 surface).

Requests and their checklists, run start/control, step decisions, the
offboarding completion ledger, and M365 status. The business logic lives in
``src.services.onboarding`` (item transitions, run-control idempotency,
step decisions) and ``src.routes.definitions`` (definition CRUD/versioning);
this module keeps the routes and the request-creation path. Route bodies,
response models, status codes, and idempotency semantics are unchanged;
``scripts/dump_openapi.py`` + the committed ``tests/openapi_snapshot.json``
keep the OpenAPI surface byte-identical across this decomposition.

Shared api.py helpers (idempotency protocol, tenant scoping) are imported
via the established ``src.api`` late-import pattern: api.py registers these
routers at the bottom of its module, after those names exist.
"""

import hashlib
import json
import os
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi import status as http_status
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src import db, definitions, idempotency, scheduler, timers, ui_session, workflow
from src.api import (  # noqa: E402
    IDEMPOTENCY_PROCESSING_TIMEOUT,
    IDEMPOTENCY_SCOPE_ONBOARDING,
    _complete_idempotency_key,
    _release_idempotency_key,
    _request_fingerprint,
    _reserve_idempotency_key,
    _validate_idempotency_key,
)
from src.auth import Actor
from src.deps import (
    _actor_tenant,
    _admin_actor,
    _global_admin_actor,
    _new_run_id,
    _operator_actor,
    _require_request_access,
    _require_workflow_run_access,
)
from src.m365_simulator import M365Simulator
from src.onboarding import initial_status_for
from src.schemas import (
    ActionExecutionOut,
    DefinitionMigrationIn,
    M365StatusOut,
    OffboardingCompletionOut,
    OffboardingStepResultOut,
    OnboardingRequestIn,
    OnboardingRequestOut,
    OnboardingStatusSummaryOut,
    WorkflowRunOut,
)
from src.services.onboarding import (
    ApprovalDecisionIn,
    _create_onboarding_request,
    _decide_workflow_step,
    _execute_item,
    _load_checklists,
    _load_request,
    _request_out,
    _run_action,
    _transition_item,
    _workflow_run_out,
)

router = APIRouter()

@router.post(
    "/onboarding-requests",
    response_model=OnboardingRequestOut,
    status_code=http_status.HTTP_201_CREATED,
)
def create_onboarding_request(
    request: OnboardingRequestIn,
    response: Response,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> OnboardingRequestOut:
    """Create an onboarding/offboarding request and generate its checklist.

    Supports ``Idempotency-Key`` with the same fenced reservation protocol as
    ``POST /tickets``: replays are 200, changed payloads are 409, and a stale
    owner that loses its lease cannot finalize the takeover's reservation.
    """
    tenant_key = _actor_tenant(actor)
    if tenant_key:
        if request.tenant_key and request.tenant_key != tenant_key:
            raise HTTPException(
                status_code=404, detail="Onboarding request not found."
            )
        request.tenant_key = tenant_key
    _validate_idempotency_key(idempotency_key)
    request_hash = _request_fingerprint(request)
    owner_token, replay = _reserve_idempotency_key(
        session,
        response,
        idempotency_key,
        request_hash,
        IDEMPOTENCY_SCOPE_ONBOARDING,
        "0",
        actor_subject=actor.subject,
    )
    if replay is not None:
        result = OnboardingRequestOut.model_validate(replay["onboarding_request"])
        record = session.get(db.OnboardingRequestRecord, result.id)
        if record is None:
            raise HTTPException(404, "Onboarding request not found.")
        _require_request_access(actor, record)
        return result

    ownership_lost = False
    try:
        result = _create_onboarding_request(request, session, actor.subject)
        if idempotency_key and owner_token is not None:
            if not _complete_idempotency_key(
                session,
                idempotency_key,
                owner_token,
                request_hash,
                IDEMPOTENCY_SCOPE_ONBOARDING,
                str(result.id),
                {"onboarding_request": result.model_dump(mode="json")},
            ):
                session.rollback()
                ownership_lost = True
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Another request took over this Idempotency-Key while it was "
                        "processing; retry the request."
                    ),
                )
        return result
    except Exception:
        if idempotency_key and owner_token is not None and not ownership_lost:
            session.rollback()
            _release_idempotency_key(session, idempotency_key, owner_token)
        raise


@router.post("/onboarding-requests/{request_id}/definition")
def migrate_definition(
    request_id: int,
    payload: DefinitionMigrationIn,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> dict[str, Any]:
    """Preview or apply a safe move to another definition version."""
    record, items = _load_request(session, request_id)
    _require_request_access(actor, record)
    if not record.definition_key:
        raise HTTPException(409, "Request is not pinned to a definition.")
    definition = definitions.get_definition(session, record.definition_key)
    target = definitions.get_version(session, record.definition_key, payload.version)
    if definition is None or target is None:
        raise HTTPException(404, "Definition version not found.")
    target_steps = {step["key"]: step for step in json.loads(target.steps_json)}
    existing = {item.step_key: item for item in items}
    added = sorted(set(target_steps) - set(existing))
    removed = sorted(set(existing) - set(target_steps))
    changed = sorted(
        key for key in set(existing) & set(target_steps)
        if (existing[key].step, existing[key].queue, existing[key].depends_on_json)
        != (
            target_steps[key]["step"],
            target_steps[key]["queue"],
            json.dumps(target_steps[key].get("depends_on", [])),
        )
    )
    diff = {
        "from_version": record.definition_version,
        "to_version": payload.version,
        "added": added,
        "removed": removed,
        "changed": changed,
        "dry_run": payload.dry_run,
        "statuses": {item.step_key: item.status for item in items},
    }
    if payload.dry_run:
        return diff
    for key in removed:
        if existing[key].status == "completed":
            raise HTTPException(
                409, f"Cannot remove completed step {key!r} during migration."
            )
        session.delete(existing[key])
    for key, step in target_steps.items():
        item = existing.get(key)
        if item is None:
            item = db.ChecklistItemRecord(
                request_id=record.id,
                step_key=key,
                step=step["step"],
                queue=step["queue"],
                requires_approval=bool(step.get("requires_approval", False)),
                action=step.get("action", "none"),
                status=initial_status_for(step),
                depends_on_json=json.dumps(step.get("depends_on", [])),
                join_mode=step.get("join", "all"),
                when_json=json.dumps(step.get("when", [])),
                sla_minutes=step.get("sla_minutes"),
                escalation_queue=step.get("escalation_queue"),
            )
            session.add(item)
        else:
            item.step = step["step"]
            item.queue = step["queue"]
            item.requires_approval = bool(step.get("requires_approval", False))
            item.action = step.get("action", "none")
            item.depends_on_json = json.dumps(step.get("depends_on", []))
            item.join_mode = step.get("join", "all")
            item.when_json = json.dumps(step.get("when", []))
            item.sla_minutes = step.get("sla_minutes")
            item.escalation_queue = step.get("escalation_queue")
    record.definition_version = payload.version
    session.flush()
    _, items = _load_request(session, record.id)
    workflow.apply_graph_transitions(
        session, items, request_id=record.id, actor_subject=actor.subject
    )
    timers.sync_timers(session, record.id)
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action="onboarding.request.definition_migrated",
        status="success",
        ticket_id=str(record.id),
        duration_ms=0,
        detail=f"definition {record.definition_key} version {payload.version}",
        actor_subject=actor.subject,
    )
    session.commit()
    return diff


@router.get("/onboarding-requests", response_model=list[OnboardingRequestOut])
def list_onboarding_requests(
    session: Session = Depends(db.get_session),
    actor: Actor = Depends(_operator_actor),
) -> list[OnboardingRequestOut]:
    """List onboarding/offboarding requests with their checklists."""
    query = select(db.OnboardingRequestRecord)
    tenant_key = _actor_tenant(actor)
    if tenant_key is not None:
        query = query.where(db.OnboardingRequestRecord.tenant_key == tenant_key)
    records = session.execute(query).scalars().all()
    grouped = _load_checklists(session, [record.id for record in records])
    return [_request_out(record, grouped.get(record.id, [])) for record in records]


@router.get("/onboarding-requests/{request_id}", response_model=OnboardingRequestOut)
def get_onboarding_request(
    request_id: int,
    session: Session = Depends(db.get_session),
    actor: Actor = Depends(_operator_actor),
) -> OnboardingRequestOut:
    record, items = _load_request(session, request_id)
    _require_request_access(actor, record)
    return _request_out(record, items)


@router.get(
    "/onboarding-requests/{request_id}/status",
    response_model=OnboardingStatusSummaryOut,
)
def get_onboarding_request_status(
    request_id: int,
    session: Session = Depends(db.get_session),
    actor: Actor = Depends(_operator_actor),
) -> OnboardingStatusSummaryOut:
    """Return a lightweight summary of request progress for automation polling."""
    record, items = _load_request(session, request_id)
    _require_request_access(actor, record)
    total = len(items)
    completed = sum(1 for item in items if item.status == "completed")
    pending_approval = sum(
        1 for item in items if item.status == "pending" and item.requires_approval
    )
    percent = round((completed / total) * 100) if total else 0
    return OnboardingStatusSummaryOut(
        id=record.id,
        request_type=record.request_type,
        employee_name=record.employee_name,
        status=record.status,
        total_steps=total,
        completed_steps=completed,
        pending_approval_steps=pending_approval,
        percent_complete=percent,
    )


@router.post("/onboarding-requests/{request_id}/run", response_model=WorkflowRunOut)
def start_workflow_run(
    request_id: int,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> WorkflowRunOut:
    """Start the DAG orchestrator for a request (idempotent per request)."""
    record, _ = _load_request(session, request_id)
    _require_request_access(actor, record)
    try:
        run = scheduler.start_run(session, request_id, actor_subject=actor.subject)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    session.commit()
    session.refresh(run)
    return _workflow_run_out(session, run)


@router.get("/onboarding-requests/{request_id}/run", response_model=WorkflowRunOut)
def get_workflow_run(
    request_id: int,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> WorkflowRunOut:
    request = session.get(db.OnboardingRequestRecord, request_id)
    if request is None:
        raise HTTPException(404, "Onboarding request not found.")
    _require_request_access(actor, request)
    run = scheduler.get_run_for_request(session, request_id)
    if run is None:
        raise HTTPException(404, "Workflow run not found.")
    return _workflow_run_out(session, run)


@router.get("/workflow-runs/{run_id}", response_model=WorkflowRunOut)
def get_workflow_run_by_id(
    run_id: int,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> WorkflowRunOut:
    run = session.get(db.WorkflowRunRecord, run_id)
    if run is None:
        raise HTTPException(404, "Workflow run not found.")
    _require_workflow_run_access(session, actor, run)
    return _workflow_run_out(session, run)


@router.get("/command-center/session")
def command_center_session(
    request: Request,
    response: Response,
    actor: Actor = Depends(_operator_actor),
) -> dict[str, Any]:
    """Verify the caller at the backend, never accept proxy-supplied roles."""
    if os.environ.get("FLOWOPS_AUTH_REQUIRED", "false").lower() != "true":
        raise HTTPException(401, "Protected authentication is required.")
    if actor.source not in ("session", "bearer"):
        raise HTTPException(401, "Authentication required.")
    response.headers["Cache-Control"] = "no-store"
    browser_session = ui_session.read_session(request) if actor.source == "session" else None
    return {
        "subject": actor.subject,
        "role": "admin" if "automation.admin" in actor.roles else "operator",
        "tenant_key": actor.tenant_key,
        "csrf_token": browser_session.csrf_token if browser_session else None,
    }


@router.get("/workflow-runs", response_model=list[WorkflowRunOut])
def list_workflow_runs(
    status_filter: str | None = Query(default=None, alias="status"),
    tenant: str | None = Query(default=None, max_length=64),
    definition: str | None = Query(default=None, max_length=128),
    created_after: datetime | None = Query(default=None),
    created_before: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> list[WorkflowRunOut]:
    query = (
        select(db.WorkflowRunRecord)
        .join(
            db.OnboardingRequestRecord,
            db.OnboardingRequestRecord.id == db.WorkflowRunRecord.request_id,
        )
        .order_by(db.WorkflowRunRecord.id.desc())
    )
    if status_filter:
        query = query.where(db.WorkflowRunRecord.status == status_filter)
    actor_tenant = _actor_tenant(actor)
    if actor_tenant is not None:
        query = query.where(db.OnboardingRequestRecord.tenant_key == actor_tenant)
    elif tenant is not None:
        query = query.where(db.OnboardingRequestRecord.tenant_key == tenant)
    if definition is not None:
        query = query.where(db.OnboardingRequestRecord.definition_key == definition)
    if created_after is not None:
        query = query.where(db.WorkflowRunRecord.created_at >= created_after)
    if created_before is not None:
        query = query.where(db.WorkflowRunRecord.created_at <= created_before)
    runs = session.execute(query.offset(offset).limit(limit)).scalars().all()
    if not runs:
        return []
    request_ids = [run.request_id for run in runs]
    run_ids = [run.id for run in runs]
    requests = {row.id: row for row in session.execute(
        select(db.OnboardingRequestRecord).where(db.OnboardingRequestRecord.id.in_(request_ids))
    ).scalars().all()}
    checklist = _load_checklists(session, request_ids)
    attempts = defaultdict(list)
    for row in session.execute(select(db.WorkflowStepRunRecord).where(
        db.WorkflowStepRunRecord.run_id.in_(run_ids)
    ).order_by(db.WorkflowStepRunRecord.id)).scalars():
        attempts[row.run_id].append(row)
    events = defaultdict(list)
    for row in session.execute(select(db.WorkflowEventRecord).where(
        db.WorkflowEventRecord.run_id.in_(run_ids)
    ).order_by(db.WorkflowEventRecord.id)).scalars():
        events[row.run_id].append(row)
    return [_workflow_run_out(session, run, loaded=(
        requests[run.request_id], checklist.get(run.request_id, []),
        attempts[run.id], events[run.id],
    )) for run in runs]


@router.post("/workflow-runs/{run_id}/steps/{item_id}/{action}", response_model=WorkflowRunOut)
def decide_workflow_step(
    run_id: int,
    item_id: int,
    action: Literal["approve", "reject"],
    payload: ApprovalDecisionIn,
    response: Response,
    actor: Actor = Depends(_admin_actor),
    session: Session = Depends(db.get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> WorkflowRunOut:
    # Authenticate and authorize before a reservation can be replayed.
    run = session.get(db.WorkflowRunRecord, run_id)
    if run is None:
        raise HTTPException(404, "Workflow run not found.")
    _require_workflow_run_access(session, actor, run)
    item = session.get(db.ChecklistItemRecord, item_id)
    if item is None or item.request_id != run.request_id:
        raise HTTPException(404, "Checklist item not found.")
    _validate_idempotency_key(idempotency_key)
    scope = idempotency.SCOPE_APPROVAL
    key = ("approval:" + hashlib.sha256(json.dumps([
        _actor_tenant(actor), actor.subject, idempotency_key
    ], separators=(",", ":")).encode()).hexdigest()) if idempotency_key else None
    fingerprint = hashlib.sha256(json.dumps([
        run_id, item_id, action, payload.reason.strip()
    ], separators=(",", ":")).encode()).hexdigest()
    owner, replay = _reserve_idempotency_key(
        session, response, key, fingerprint, scope, str(run.request_id), actor_subject=actor.subject
    )
    if replay is not None:
        return WorkflowRunOut.model_validate(replay["workflow_run"])
    try:
        result = _decide_workflow_step(session, run_id, item_id, action, payload, actor)
        if key and owner:
            if not _complete_idempotency_key(
                session, key, owner, fingerprint, scope, str(result.request_id),
                {"workflow_run": result.model_dump(mode="json")},
            ):
                raise HTTPException(409, "Decision reservation changed; retry.")
        else:
            session.commit()
        return result
    except Exception:
        session.rollback()
        if key and owner:
            _release_idempotency_key(session, key, owner)
        raise



@router.post("/workflow-runs/{run_id}/pause", response_model=WorkflowRunOut)
def pause_workflow_run(
    run_id: int,
    response: Response,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> WorkflowRunOut:
    return _run_action(session, run_id, actor, "pause", response, idempotency_key)


@router.post("/workflow-runs/{run_id}/resume", response_model=WorkflowRunOut)
def resume_workflow_run(
    run_id: int,
    response: Response,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> WorkflowRunOut:
    return _run_action(session, run_id, actor, "resume", response, idempotency_key)


@router.post("/workflow-runs/{run_id}/cancel", response_model=WorkflowRunOut)
def cancel_workflow_run(
    run_id: int,
    response: Response,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> WorkflowRunOut:
    return _run_action(session, run_id, actor, "cancel", response, idempotency_key)


@router.post("/onboarding-requests/{request_id}/items/{item_id}/execute")
def execute_item(
    request_id: int,
    item_id: int,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> OnboardingRequestOut:
    """Execute a checklist action through the configured Microsoft adapter."""
    return _execute_item(session, request_id, item_id, actor)

@router.get("/onboarding-requests/{request_id}/action-executions", response_model=list[ActionExecutionOut])
def list_action_executions(
    request_id: int,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> list[ActionExecutionOut]:
    """List durable action attempts for one onboarding request."""
    record, _ = _load_request(session, request_id)
    _require_request_access(actor, record)
    records = session.execute(
        select(db.ActionExecutionRecord)
        .where(db.ActionExecutionRecord.request_id == request_id)
        .order_by(db.ActionExecutionRecord.id)
    ).scalars().all()
    return [ActionExecutionOut.model_validate(record, from_attributes=True) for record in records]


@router.post("/onboarding-requests/{request_id}/items/{item_id}/approve")
def approve_item(
    request_id: int,
    item_id: int,
    request: Request,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> OnboardingRequestOut:
    """Approve a checklist step that requires human sign-off."""
    record, _ = _load_request(session, request_id)
    _require_request_access(actor, record)
    return _transition_item(
        session, request_id, item_id, request, "approve", actor.subject
    )


@router.post("/onboarding-requests/{request_id}/items/{item_id}/complete")
def complete_item(
    request_id: int,
    item_id: int,
    request: Request,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> OnboardingRequestOut:
    """Mark a checklist step completed (after approval if required).

    v0.6 placeholder: steps with an attached automation run it now — e.g.
    ``m365_provision`` calls the simulated Microsoft 365 backend and stores
    the returned (simulated) user id in the step's result.

    Offboarding requests are rejected here with 409: they use the dedicated
    ledger-backed endpoint below instead (see its docstring for why the two
    flows are not unified yet).
    """
    record, _ = _load_request(session, request_id)
    _require_request_access(actor, record)
    if record.request_type == "offboarding":
        raise HTTPException(
            409,
            "Offboarding steps must be completed via "
            "POST /offboarding-requests/{checklist_id}/steps/{step_key}/complete "
            "(idempotent completion ledger).",
        )
    return _transition_item(
        session, request_id, item_id, request, "complete", actor.subject
    )


# Module-level *simulated* side-effect executor for offboarding steps.
# Tests monkeypatch this to count calls or force failures; a future real
# version would call Microsoft Graph (with credentials configured outside
# chat). Everything it returns is clearly labelled as simulated.
def _simulate_offboarding_action(step_key: str, employee_email: str) -> dict:
    """Return a fake but realistic-looking result for an offboarding step."""
    return {
        "simulated": True,
        "action": step_key,
        "target": employee_email,
        "detail": f"Simulated '{step_key}' completed for {employee_email}.",
    }


offboarding_executor = _simulate_offboarding_action


@router.post(
    "/offboarding-requests/{checklist_id}/steps/{step_key}/complete",
    response_model=OffboardingCompletionOut,
)
def complete_offboarding_step(
    checklist_id: int,
    step_key: str,
    response: Response,
    actor: Actor = Depends(_admin_actor),
    session: Session = Depends(db.get_session),
) -> OffboardingCompletionOut:
    """Idempotently complete one offboarding step (v0.6.1 hardening).

    The ledger row is claimed *before* the simulated side effect runs, so two
    concurrent callers cannot both execute it. A completed row replays its
    stored result; a fresh ``running`` row returns 409; a stale claim older
    than the five-minute recovery window can be taken over with a new owner
    token. On executor failure the claim is removed so a caller can retry.

    Offboarding is destructive and therefore admin-only. Unknown step keys
    return 422, onboarding checklists 409, missing checklists 404, and closed
    or abandoned checklists 409. Every path writes an execution log with the
    authenticated actor.
    """
    started = time.perf_counter()
    record = session.get(db.OnboardingRequestRecord, checklist_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Offboarding checklist not found.")
    _require_request_access(actor, record)
    items = session.execute(
        select(db.ChecklistItemRecord).where(
            db.ChecklistItemRecord.request_id == checklist_id
        )
    ).scalars().all()

    def log(action_suffix: str, status: str, detail: str) -> None:
        db.log_execution(
            session,
            run_id=_new_run_id(),
            action=f"offboarding.step_{action_suffix}",
            status=status,
            duration_ms=int((time.perf_counter() - started) * 1000),
            detail=f"checklist #{checklist_id} step '{step_key}': {detail}",
            actor_subject=actor.subject,
        )

    def replay(existing: db.OffboardingCompletionRecord, reason: str) -> OffboardingCompletionOut:
        stored = json.loads(existing.result_json)
        log("replayed", "success", reason)
        response.headers["X-Offboarding-Replayed"] = "true"
        response.status_code = http_status.HTTP_200_OK
        return OffboardingCompletionOut.model_validate(stored)

    if record.request_type != "offboarding":
        log("rejected", "failure", "request is not an offboarding checklist")
        session.commit()
        raise HTTPException(
            409,
            "This checklist is an onboarding request; offboarding "
            "completions only apply to offboarding checklists.",
        )

    known_keys = [item.step_key for item in items]
    if step_key not in known_keys:
        log("rejected", "failure", "unknown step key for this checklist")
        session.commit()
        raise HTTPException(
            422,
            f"Unknown step_key {step_key!r} for checklist #{checklist_id}. "
            f"Expected one of {known_keys}.",
        )

    if record.status in ("closed", "abandoned"):
        log("rejected", "failure", f"checklist is '{record.status}'")
        session.commit()
        raise HTTPException(
            409,
            f"Cannot complete steps on a '{record.status}' checklist.",
        )

    table = db.OffboardingCompletionRecord
    now = db.utcnow()
    existing = session.execute(
        select(table).where(
            table.checklist_id == checklist_id,
            table.step_key == step_key,
        )
    ).scalar_one_or_none()

    if existing is not None:
        state = existing.status or "completed"  # NULL = legacy completed row
        if state == "completed":
            return replay(existing, f"returning stored result from ledger #{existing.id}")
        if state != "running":
            log("rejected", "failure", f"ledger row is in state '{state}'")
            session.commit()
            raise HTTPException(409, "Offboarding ledger is in an unexpected state.")
        stale_before = now - IDEMPOTENCY_PROCESSING_TIMEOUT
        claimed_at = existing.claimed_at
        stale = claimed_at is None
        if claimed_at is not None:
            if claimed_at.tzinfo is None:
                claimed_at = claimed_at.replace(tzinfo=timezone.utc)
            stale = claimed_at < stale_before
        if not stale:
            log("rejected", "failure", "step is already running")
            session.commit()
            raise HTTPException(
                409, "Offboarding step is already running; retry later."
            )
        owner_token = uuid.uuid4().hex
        takeover = session.execute(
            update(table)
            .execution_options(synchronize_session=False)
            .where(
                table.id == existing.id,
                table.status == "running",
                or_(table.claimed_at.is_(None), table.claimed_at < stale_before),
            )
            .values(owner_token=owner_token, claimed_at=now)
        )
        if takeover.rowcount != 1:
            session.rollback()
            raise HTTPException(
                409, "Offboarding claim changed hands; refresh and retry."
            )
        session.commit()
        claim = existing
    else:
        owner_token = uuid.uuid4().hex
        claim = table(
            checklist_id=checklist_id,
            step_key=step_key,
            status="running",
            owner_token=owner_token,
            claimed_at=now,
            result_json="{}",
        )
        session.add(claim)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            winner = session.execute(
                select(table).where(
                    table.checklist_id == checklist_id,
                    table.step_key == step_key,
                )
            ).scalar_one_or_none()
            if winner is not None and (winner.status or "completed") == "completed":
                return replay(winner, f"lost the claim race; replayed ledger #{winner.id}")
            log("rejected", "failure", "lost the claim race to an in-flight request")
            session.commit()
            raise HTTPException(
                409, "Offboarding step is already running; retry later."
            ) from exc

    item = next(i for i in items if i.step_key == step_key)
    missing = workflow.missing_prerequisites(item, items)
    if missing:
        log("rejected", "failure", f"unmet prerequisites: {', '.join(missing)}")
        session.execute(
            delete(table).where(
                table.id == claim.id,
                table.status == "running",
                table.owner_token == owner_token,
            )
        )
        session.commit()
        raise HTTPException(
            409,
            f"Step '{step_key}' is blocked by incomplete prerequisites: "
            f"{', '.join(missing)}.",
        )
    if item.status == "skipped":
        log("rejected", "failure", "step was skipped by a predicate")
        session.execute(
            delete(table).where(
                table.id == claim.id,
                table.status == "running",
                table.owner_token == owner_token,
            )
        )
        session.commit()
        raise HTTPException(
            409, f"Step '{step_key}' was skipped by a workflow predicate."
        )
    if item.requires_approval and item.status != "approved":
        log("rejected", "failure", f"step requires approval (status '{item.status}')")
        session.execute(
            delete(table).where(
                table.id == claim.id,
                table.status == "running",
                table.owner_token == owner_token,
            )
        )
        session.commit()
        raise HTTPException(
            409,
            f"Step '{step_key}' requires approval before completion "
            f"(current status: '{item.status}'). Approve it via "
            "POST /onboarding-requests/{request_id}/items/{item_id}/approve.",
        )

    try:
        result_payload = offboarding_executor(step_key, record.employee_email)
        body = OffboardingCompletionOut(
            checklist_id=checklist_id,
            employee_name=record.employee_name,
            step=OffboardingStepResultOut(
                step_key=step_key,
                status="completed",
                executor_called=True,
                executed_at=db.utcnow(),
            ),
        )
        stored_json = json.dumps(
            {**body.model_dump(mode="json"), "simulated_result": result_payload},
            separators=(",", ":"),
        )
        completed = session.execute(
            update(table)
            .execution_options(synchronize_session=False)
            .where(
                table.id == claim.id,
                table.status == "running",
                table.owner_token == owner_token,
            )
            .values(
                status="completed",
                result_json=stored_json,
                completed_at=db.utcnow(),
                owner_token=None,
                claimed_at=None,
            )
        )
        if completed.rowcount != 1:
            session.rollback()
            raise HTTPException(
                409, "Offboarding claim was taken over; refresh and retry."
            )
        item.status = "completed"
        log("executed", "success", f"simulated executor result: {result_payload['action']}")
        all_items = session.execute(
            select(db.ChecklistItemRecord).where(
                db.ChecklistItemRecord.request_id == checklist_id
            )
        ).scalars().all()
        workflow.apply_graph_transitions(
            session, all_items, request_id=checklist_id, actor_subject=actor.subject
        )
        timers.sync_timers(session, checklist_id)
        all_items = session.execute(
            select(db.ChecklistItemRecord).where(
                db.ChecklistItemRecord.request_id == checklist_id
            )
        ).scalars().all()
        if all(i.status == "completed" for i in all_items) and record.status != "completed":
            record.status = "completed"
            db.log_execution(
                session,
                run_id=_new_run_id(),
                action="onboarding.request.completed",
                status="success",
                duration_ms=0,
                detail=f"all checklist steps done for request #{record.id}",
                actor_subject=actor.subject,
            )
        session.commit()
        response.status_code = http_status.HTTP_201_CREATED
        return body
    except Exception:
        session.rollback()
        session.execute(
            delete(table).where(
                table.id == claim.id,
                table.status == "running",
                table.owner_token == owner_token,
            )
        )
        session.commit()
        raise


@router.get("/onboarding-requests/m365-status/{user_id}", response_model=M365StatusOut)
def get_m365_status(
    user_id: str,
    actor: Actor = Depends(_global_admin_actor),
) -> M365StatusOut:
    """Look up a *simulated* M365 provisioning status by user id.

    This never contacts real Microsoft services; see src/m365_simulator.py.
    """
    return M365StatusOut.model_validate(M365Simulator().get_status(user_id))
