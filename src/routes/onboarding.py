"""Onboarding/offboarding workflow routes (extracted from src/api.py).

Workflow definitions CRUD/validate/publish/diff/migrate, onboarding and
offboarding requests, run start/control, step decisions, the durable item
transitions, the offboarding completion ledger, and M365 status. Route
bodies, response models, status codes, and idempotency semantics are
unchanged; ``scripts/dump_openapi.py`` + the committed
``tests/openapi_snapshot.json`` keep the OpenAPI surface byte-identical
across this decomposition.

Shared api.py helpers (idempotency protocol, tenant scoping, action
configuration) are imported via the established ``src.routes`` late-import
pattern: api.py registers this router at the bottom of its module, after
those names exist.
"""

import hashlib
import json
import os
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi import status as http_status
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src import db, definitions, idempotency, scheduler, timers, ui_session, workflow
from src.onboarding import checklist_for, initial_status_for
from src.actions import ActionAdapter, ActionError, action_for_step, adapter_from_environment
from src.auth import Actor
from src.deps import (
    _admin_actor,
    _actor_tenant,
    _global_admin_actor,
    _new_run_id,
    _operator_actor,
    _require_request_access,
    _require_workflow_run_access,
)
from src.m365_simulator import M365Simulator
from src.schemas import (
    ActionExecutionOut,
    ChecklistItemOut,
    DefinitionCreateIn,
    DefinitionMigrationIn,
    DefinitionPublishIn,
    DefinitionVersionIn,
    DefinitionVersionOut,
    M365StatusOut,
    OffboardingCompletionOut,
    OffboardingStepResultOut,
    OnboardingRequestIn,
    OnboardingRequestOut,
    OnboardingStatusSummaryOut,
    WorkflowDefinitionOut,
    WorkflowEventOut,
    WorkflowRunOut,
    WorkflowStepRunOut,
)

from src.api import (  # noqa: E402
    IDEMPOTENCY_PROCESSING_TIMEOUT,
    IDEMPOTENCY_SCOPE_ONBOARDING,
    _action_attempts,
    _complete_idempotency_key,
    _notify_action_failure,
    _release_idempotency_key,
    _request_fingerprint,
    _reserve_idempotency_key,
    _validate_idempotency_key,
)


router = APIRouter()

# v0.6 action boundary: the adapter that executes checklist actions. Tests
# monkeypatch this module attribute with failing/succeeding doubles.
ACTION_ADAPTER: ActionAdapter = adapter_from_environment()

# Routes: onboarding/offboarding workflows


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


def _definition_out(
    session: Session, definition: db.WorkflowDefinitionRecord
) -> WorkflowDefinitionOut:
    versions = definitions.list_versions(session, definition.key)
    return WorkflowDefinitionOut(
        key=definition.key,
        name=definition.name,
        request_type=definition.request_type,
        status=definition.status,
        current_version=definition.current_version,
        versions=[
            DefinitionVersionOut(
                version=row.version,
                checksum=row.checksum,
                steps=json.loads(row.steps_json),
                created_by=row.created_by,
                created_at=row.created_at,
                published_at=row.published_at,
            )
            for row in versions
        ],
    )


def _definition_mutation(
    session: Session, response: Response, actor: Actor, operation: str,
    definition_key: str, payload: BaseModel, idempotency_key: Optional[str],
) -> WorkflowDefinitionOut:
    """Use the existing fenced transaction for all definition mutations."""
    _validate_idempotency_key(idempotency_key)
    scope = idempotency.SCOPE_DEFINITION
    key = ("definition:" + hashlib.sha256(json.dumps(
        [actor.subject, idempotency_key], separators=(",", ":")
    ).encode()).hexdigest()) if idempotency_key else None
    fingerprint = hashlib.sha256(json.dumps(
        [operation, definition_key, payload.model_dump(mode="json")],
        sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    owner, replay = _reserve_idempotency_key(
        session, response, key, fingerprint, scope, definition_key, actor_subject=actor.subject
    )
    if replay is not None:
        return WorkflowDefinitionOut.model_validate(replay["definition"])
    try:
        if operation == "create":
            definition = definitions.create_definition(
                session, key=payload.key, name=payload.name,
                request_type=payload.request_type, steps=payload.steps,
                actor=actor.subject,
            )
        elif operation == "version":
            definitions.create_version(session, definition_key, payload.steps, actor=actor.subject)
            definition = definitions.get_definition(session, definition_key)
        else:
            definition = definitions.publish_version(
                session, definition_key, payload.version, actor=actor.subject
            )
        if definition is None:
            raise HTTPException(404, "Definition not found.")
        result = _definition_out(session, definition)
        db.log_execution(
            session, run_id=_new_run_id(), action=f"workflow.definition.{operation}",
            status="success", detail=f"definition={definition.key}",
            actor_subject=actor.subject,
        )
        if key and owner:
            if not _complete_idempotency_key(
                session, key, owner, fingerprint, scope, definition.key,
                {"definition": result.model_dump(mode="json")},
            ):
                raise HTTPException(409, "Definition reservation changed; retry.")
        else:
            session.commit()
        return result
    except Exception as error:
        session.rollback()
        if key and owner:
            _release_idempotency_key(session, key, owner)
        if isinstance(error, definitions.DefinitionError):
            raise HTTPException(422, str(error)) from error
        if isinstance(error, IntegrityError):
            raise HTTPException(409, "Concurrent definition change; refresh and retry.") from error
        raise


class DefinitionValidationIn(BaseModel):
    request_type: Literal["onboarding", "offboarding"]
    steps: list[dict[str, Any]] = Field(min_length=1, max_length=200)


class DefinitionValidationOut(BaseModel):
    steps: list[dict[str, Any]]
    checksum: str


@router.post("/workflow-definitions/validate", response_model=DefinitionValidationOut)
def validate_workflow_definition(
    payload: DefinitionValidationIn,
    actor: Actor = Depends(_global_admin_actor),
) -> DefinitionValidationOut:
    """Preview the authoritative DAG validator without writing any records."""
    try:
        steps = definitions.validate_steps(payload.steps, payload.request_type)
    except definitions.DefinitionError as error:
        raise HTTPException(422, str(error)) from error
    return DefinitionValidationOut(steps=steps, checksum=definitions.checksum_for(steps))


@router.post("/workflow-definitions", response_model=WorkflowDefinitionOut)
def create_workflow_definition(
    payload: DefinitionCreateIn,
    response: Response,
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> WorkflowDefinitionOut:
    return _definition_mutation(session, response, actor, "create", payload.key, payload, idempotency_key)


@router.put("/workflow-definitions/{key}", response_model=WorkflowDefinitionOut)
def create_workflow_definition_version(
    key: str,
    payload: DefinitionVersionIn,
    response: Response,
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> WorkflowDefinitionOut:
    return _definition_mutation(session, response, actor, "version", key, payload, idempotency_key)


@router.post("/workflow-definitions/{key}/publish", response_model=WorkflowDefinitionOut)
def publish_workflow_definition(
    key: str,
    payload: DefinitionPublishIn,
    response: Response,
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> WorkflowDefinitionOut:
    return _definition_mutation(session, response, actor, "publish", key, payload, idempotency_key)


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


@router.get("/workflow-definitions", response_model=list[WorkflowDefinitionOut])
def list_workflow_definitions(
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
) -> list[WorkflowDefinitionOut]:
    return [
        _definition_out(session, definition)
        for definition in definitions.list_definitions(session)
    ]


@router.get("/workflow-definitions/{key}/diff")
def compare_workflow_definition_versions(
    key: str,
    from_version: int = Query(alias="from", ge=1),
    to_version: int = Query(alias="to", ge=1),
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
) -> dict[str, Any]:
    previous = definitions.get_version(session, key, from_version)
    current = definitions.get_version(session, key, to_version)
    if previous is None or current is None:
        raise HTTPException(404, "Definition version not found.")
    before = {step["key"]: step for step in json.loads(previous.steps_json)}
    after = {step["key"]: step for step in json.loads(current.steps_json)}
    return {
        "from_version": from_version,
        "to_version": to_version,
        "added": sorted(after.keys() - before.keys()),
        "removed": sorted(before.keys() - after.keys()),
        "changed": sorted(k for k in before.keys() & after.keys() if before[k] != after[k]),
    }


@router.get("/workflow-definitions/{key}", response_model=WorkflowDefinitionOut)
def get_workflow_definition(
    key: str,
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
) -> WorkflowDefinitionOut:
    definition = definitions.get_definition(session, key)
    if definition is None:
        raise HTTPException(404, "Definition not found.")
    return _definition_out(session, definition)


def _create_onboarding_request(
    request: OnboardingRequestIn,
    session: Session,
    actor_subject: Optional[str] = None,
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
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
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


def _workflow_run_out(
    session: Session, run: db.WorkflowRunRecord, *,
    loaded: Optional[tuple] = None,
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
    status_filter: Optional[str] = Query(default=None, alias="status"),
    tenant: Optional[str] = Query(default=None, max_length=64),
    definition: Optional[str] = Query(default=None, max_length=128),
    created_after: Optional[datetime] = Query(default=None),
    created_before: Optional[datetime] = Query(default=None),
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


def _run_action(
    session: Session, run_id: int, actor: Actor, action: str,
    response: Response, idempotency_key: Optional[str],
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


@router.post("/workflow-runs/{run_id}/steps/{item_id}/{action}", response_model=WorkflowRunOut)
def decide_workflow_step(
    run_id: int,
    item_id: int,
    action: Literal["approve", "reject"],
    payload: ApprovalDecisionIn,
    response: Response,
    actor: Actor = Depends(_admin_actor),
    session: Session = Depends(db.get_session),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
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


@router.post("/workflow-runs/{run_id}/pause", response_model=WorkflowRunOut)
def pause_workflow_run(
    run_id: int,
    response: Response,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> WorkflowRunOut:
    return _run_action(session, run_id, actor, "pause", response, idempotency_key)


@router.post("/workflow-runs/{run_id}/resume", response_model=WorkflowRunOut)
def resume_workflow_run(
    run_id: int,
    response: Response,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> WorkflowRunOut:
    return _run_action(session, run_id, actor, "resume", response, idempotency_key)


@router.post("/workflow-runs/{run_id}/cancel", response_model=WorkflowRunOut)
def cancel_workflow_run(
    run_id: int,
    response: Response,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> WorkflowRunOut:
    return _run_action(session, run_id, actor, "cancel", response, idempotency_key)


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
    actor_subject: Optional[str] = None,
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
        except IntegrityError:
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
            )

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


# ---------------------------------------------------------------------------
