"""Event-driven trigger CRUD, signed inbound webhook, and operator pages (v1.4)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi import status as http_status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, StrictBool, StrictInt, StrictStr, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from src import db, definitions, jobs, triggers, web
from src.auth import Actor
from src.deps import _global_admin_actor, _new_run_id

router = APIRouter(tags=["triggers"])
MAX_TRIGGER_WEBHOOK_BYTES = 1_048_576

class EventSourceCreateIn(BaseModel):
    key: StrictStr = Field(description="Unique identifier for the source.")
    name: StrictStr = Field(description="Human-readable name.")
    secret: StrictStr = Field(
        min_length=triggers.MIN_WEBHOOK_SECRET_BYTES,
        max_length=128,
        description="Random shared secret for HMAC signature verification (at least 32 bytes).",
    )
    tenant_key: Optional[StrictStr] = Field(default=None, max_length=64)
    global_scope: StrictBool = False

    @field_validator("secret")
    @classmethod
    def valid_secret(cls, value: str) -> str:
        if len(value.encode("utf-8")) < triggers.MIN_WEBHOOK_SECRET_BYTES:
            raise ValueError("secret must contain at least 32 UTF-8 bytes")
        if value != value.strip():
            raise ValueError("secret must not have leading or trailing whitespace")
        return value


class EventSourceUpdateIn(BaseModel):
    secret: Optional[StrictStr] = Field(default=None, min_length=32, max_length=128)
    tenant_key: Optional[StrictStr] = Field(default=None, max_length=64)
    active: Optional[StrictBool] = None

    @field_validator("secret")
    @classmethod
    def valid_secret(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if len(value.encode("utf-8")) < triggers.MIN_WEBHOOK_SECRET_BYTES:
            raise ValueError("secret must contain at least 32 UTF-8 bytes")
        if value != value.strip():
            raise ValueError("secret must not have leading or trailing whitespace")
        return value


class EventSourceOut(BaseModel):
    key: str
    name: str
    active: bool
    created_at: datetime
    tenant_key: Optional[str] = None


def _validate_source_tenant(session: Session, tenant_key: Optional[str]) -> None:
    if tenant_key is None:
        return
    tenant = session.get(db.TenantRecord, tenant_key)
    if tenant is None or not tenant.active:
        raise HTTPException(status_code=404, detail="Active tenant not found.")


@router.get("/event-sources", response_model=list[EventSourceOut])
def list_event_sources(
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
) -> list[EventSourceOut]:
    sources = session.execute(select(db.EventSourceRecord)).scalars().all()
    return [
        EventSourceOut(
            key=s.key,
            name=s.name,
            tenant_key=s.tenant_key,
            active=s.active,
            created_at=s.created_at,
        )
        for s in sources
    ]


@router.post("/event-sources", response_model=EventSourceOut, status_code=http_status.HTTP_201_CREATED)
def create_event_source(
    payload: EventSourceCreateIn,
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
) -> EventSourceOut:
    if session.get(db.EventSourceRecord, payload.key):
        raise HTTPException(status_code=409, detail="Event source key already exists.")
    if payload.tenant_key is None and not payload.global_scope:
        raise HTTPException(
            status_code=422,
            detail="Set tenant_key or explicitly opt into global_scope.",
        )
    if payload.tenant_key is not None and payload.global_scope:
        raise HTTPException(
            status_code=422,
            detail="A source cannot be both tenant-scoped and global.",
        )
    _validate_source_tenant(session, payload.tenant_key)
    source = db.EventSourceRecord(
        key=payload.key,
        name=payload.name,
        secret=payload.secret,
        tenant_key=payload.tenant_key,
        active=True,
        created_at=db.utcnow(),
    )
    session.add(source)
    session.flush()
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action="event_source.created",
        status="success",
        ticket_id=None,
        duration_ms=0,
        detail=f"key={payload.key}, tenant_key={payload.tenant_key or 'global'}",
        actor_subject=actor.subject,
    )
    session.commit()
    return EventSourceOut(
        key=source.key,
        name=source.name,
        tenant_key=source.tenant_key,
        active=source.active,
        created_at=source.created_at,
    )


@router.patch("/event-sources/{key}", response_model=EventSourceOut)
def update_event_source(
    key: str,
    payload: EventSourceUpdateIn,
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
) -> EventSourceOut:
    source = session.get(db.EventSourceRecord, key)
    if source is None:
        raise HTTPException(status_code=404, detail="Event source not found.")
    if not payload.model_fields_set:
        raise HTTPException(status_code=422, detail="At least one source field is required.")
    if "tenant_key" in payload.model_fields_set:
        if payload.tenant_key != source.tenant_key:
            existing_trigger = session.execute(
                select(db.TriggerRecord.key)
                .where(db.TriggerRecord.source_key == key)
                .limit(1)
            ).first()
            if existing_trigger is not None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Tenant scope cannot change after triggers use this source; "
                        "create a replacement source and triggers."
                    ),
                )
        _validate_source_tenant(session, payload.tenant_key)
        source.tenant_key = payload.tenant_key
    if payload.secret is not None:
        source.secret = payload.secret
    if payload.active is not None:
        source.active = payload.active
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action="event_source.updated",
        status="success",
        ticket_id=None,
        duration_ms=0,
        detail=(
            f"key={key}, tenant_key={source.tenant_key or 'global'}, "
            f"fields={','.join(sorted(payload.model_fields_set))}"
        ),
        actor_subject=actor.subject,
    )
    session.commit()
    return EventSourceOut(
        key=source.key,
        name=source.name,
        tenant_key=source.tenant_key,
        active=source.active,
        created_at=source.created_at,
    )


class TriggerCreateIn(BaseModel):
    key: StrictStr = Field(description="Unique identifier for the trigger.")
    name: StrictStr = Field(description="Human-readable name.")
    source_key: StrictStr = Field(description="Event source key.")
    event_type: StrictStr = Field(description="Event type to match.")
    target_type: StrictStr = Field(description="Target: onboarding_request, offboarding_request, or ticket.")
    definition_key: Optional[StrictStr] = Field(default=None, description="Definition key for onboarding/offboarding.")
    mapping_json: dict[str, Any] = Field(description="Mapping from event to request fields.")
    schedule_kind: StrictStr = Field(default="webhook", description="webhook, interval, or daily.")
    interval_seconds: Optional[StrictInt] = Field(default=None, description="Interval in seconds for interval schedules.")
    daily_at_utc: Optional[StrictStr] = Field(default=None, description="HH:MM UTC for daily schedules.")

    @field_validator("target_type")
    @classmethod
    def valid_target_type(cls, value: str) -> str:
        if value not in triggers.VALID_TARGET_TYPES:
            raise ValueError(f"target_type must be one of {triggers.VALID_TARGET_TYPES}")
        return value

    @field_validator("schedule_kind")
    @classmethod
    def valid_schedule_kind(cls, value: str) -> str:
        if value not in triggers.VALID_SCHEDULE_KINDS:
            raise ValueError(f"schedule_kind must be one of {triggers.VALID_SCHEDULE_KINDS}")
        return value


class TriggerUpdateIn(BaseModel):
    name: Optional[StrictStr] = None
    active: Optional[StrictBool] = None
    mapping_json: Optional[dict[str, Any]] = None


class TriggerOut(BaseModel):
    key: str
    name: str
    source_key: str
    event_type: str
    target_type: str
    definition_key: Optional[str] = None
    mapping_json: dict[str, Any]
    schedule_kind: str
    interval_seconds: Optional[int] = None
    daily_at_utc: Optional[str] = None
    active: bool
    last_fired_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class TriggerDeliveryOut(BaseModel):
    id: int
    trigger_key: str
    event_id: str
    payload: dict[str, Any]
    status: str
    request_id: Optional[str] = None
    error: Optional[str] = None
    received_at: datetime
    processed_at: Optional[datetime] = None


async def _read_raw_body(request: Request) -> bytes:
    """Read bounded webhook bytes without buffering an untrusted full body."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="Invalid Content-Length.") from error
        if declared_length < 0:
            raise HTTPException(status_code=400, detail="Invalid Content-Length.")
        if declared_length > MAX_TRIGGER_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="Webhook body exceeds the 1 MiB limit.")

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_TRIGGER_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="Webhook body exceeds the 1 MiB limit.")
        body.extend(chunk)
    return bytes(body)


@router.post("/webhooks/triggers/{trigger_key}")
def trigger_webhook(
    trigger_key: str,
    request: Request,
    x_flowops_timestamp: Optional[str] = Header(default=None, alias="X-FlowOps-Timestamp"),
    x_flowops_signature: Optional[str] = Header(default=None, alias="X-FlowOps-Signature"),
    x_flowops_event_id: Optional[str] = Header(default=None, alias="X-FlowOps-Event-ID"),
    body_bytes: bytes = Depends(_read_raw_body),
    session: Session = Depends(db.get_session),
) -> dict[str, Any]:
    """Signed inbound webhook for event-driven triggers.

    Headers:
    * X-FlowOps-Timestamp: Unix seconds
    * X-FlowOps-Signature: sha256=<hex HMAC>
    * X-FlowOps-Event-ID: Optional mirror of the signed body's event_id

    HMAC input: timestamp + "." + raw request body.
    """
    trigger = session.get(db.TriggerRecord, trigger_key)
    if trigger is None or not trigger.active:
        raise HTTPException(status_code=404, detail="Trigger not found or inactive.")

    source = session.get(db.EventSourceRecord, trigger.source_key)
    if source is None or not source.active:
        raise HTTPException(status_code=404, detail="Event source not found or inactive.")
    if source.tenant_key is not None:
        tenant = session.get(db.TenantRecord, source.tenant_key)
        if tenant is None or not tenant.active:
            raise HTTPException(status_code=404, detail="Event source not found or inactive.")

    # Parse timestamp
    try:
        timestamp = int(x_flowops_timestamp) if x_flowops_timestamp else 0
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid timestamp.")

    # Verify signature
    if not triggers.verify_signature(body_bytes, x_flowops_signature or "", source.secret, timestamp):
        raise HTTPException(status_code=401, detail="Invalid signature or expired timestamp.")

    # Parse payload
    try:
        payload = json.loads(body_bytes.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=422, detail="Malformed JSON payload.")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Webhook payload must be a JSON object.")

    # The event ID must be in the signed body. A header may mirror it for
    # compatibility, but it cannot override the value covered by the HMAC.
    event_id = payload.get("event_id")
    if not event_id:
        raise HTTPException(status_code=422, detail="Event ID is required in the signed body.")
    if not isinstance(event_id, str) or len(event_id) > triggers.MAX_EVENT_ID_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"Event ID must be a string of at most {triggers.MAX_EVENT_ID_LENGTH} characters.",
        )
    if x_flowops_event_id is not None and x_flowops_event_id != event_id:
        raise HTTPException(status_code=422, detail="Event ID header must match the signed body.")

    # Check for replay
    existing = session.execute(
        select(db.TriggerDeliveryRecord).where(
            db.TriggerDeliveryRecord.trigger_key == trigger_key,
            db.TriggerDeliveryRecord.event_id == event_id,
        )
    ).scalar_one_or_none()

    if existing:
        return {
            "status": "replayed",
            "delivery_id": existing.id,
            "delivery_status": existing.status,
            "request_id": existing.request_id,
        }

    # Record delivery
    delivery = triggers.record_delivery(
        session,
        trigger_key=trigger.key,
        event_id=event_id,
        payload=payload,
        status="queued",
    )

    # Enqueue job for processing
    jobs.enqueue_job(
        session,
        job_type=jobs.JOB_TRIGGER_EXECUTE,
        payload={
            "delivery_id": delivery.id,
            "trigger_key": trigger.key,
            "event_id": event_id,
            "event_payload": payload,
        },
        label=f"trigger:{trigger.key}",
    )

    # Log audit event
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action="trigger.delivery.received",
        status="success",
        ticket_id=None,
        duration_ms=0,
        detail=f"trigger={trigger.key}, event_id={event_id}",
        actor_subject=f"trigger:{trigger.key}",
        correlation_id=getattr(request.state, "correlation_id", None),
    )

    session.commit()

    return {
        "status": "queued",
        "delivery_id": delivery.id,
        "event_id": event_id,
    }


@router.get("/triggers", response_model=list[TriggerOut])
def list_triggers(
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
) -> list[TriggerOut]:
    trigger_records = session.execute(select(db.TriggerRecord)).scalars().all()
    return [
        TriggerOut(
            key=t.key,
            name=t.name,
            source_key=t.source_key,
            event_type=t.event_type,
            target_type=t.target_type,
            definition_key=t.definition_key,
            mapping_json=json.loads(t.mapping_json),
            schedule_kind=t.schedule_kind,
            interval_seconds=t.interval_seconds,
            daily_at_utc=t.daily_at_utc,
            active=t.active,
            last_fired_at=t.last_fired_at,
            created_at=t.created_at,
            updated_at=t.updated_at,
        )
        for t in trigger_records
    ]


@router.post("/triggers", response_model=TriggerOut, status_code=http_status.HTTP_201_CREATED)
def create_trigger(
    payload: TriggerCreateIn,
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
) -> TriggerOut:
    if session.get(db.TriggerRecord, payload.key):
        raise HTTPException(status_code=409, detail="Trigger key already exists.")
    source = session.get(db.EventSourceRecord, payload.source_key)
    if source is None or not source.active:
        raise HTTPException(status_code=404, detail="Event source not found.")
    _validate_source_tenant(session, source.tenant_key)
    if payload.target_type in ("onboarding_request", "offboarding_request") and not payload.definition_key:
        raise HTTPException(status_code=422, detail="definition_key is required for onboarding/offboarding targets.")
    if payload.definition_key:
        definition = definitions.get_definition(session, payload.definition_key)
        if definition is None:
            raise HTTPException(status_code=404, detail="Definition not found.")
        if definition.status != "published":
            raise HTTPException(status_code=422, detail="Definition must be published.")
    if payload.schedule_kind == "interval" and not payload.interval_seconds:
        raise HTTPException(status_code=422, detail="interval_seconds is required for interval schedules.")
    if payload.schedule_kind == "daily" and not payload.daily_at_utc:
        raise HTTPException(status_code=422, detail="daily_at_utc is required for daily schedules.")
    try:
        triggers.validate_mapping(payload.mapping_json)
    except triggers.MappingError as error:
        raise HTTPException(status_code=422, detail=f"Invalid mapping: {error}") from error

    trigger = db.TriggerRecord(
        key=payload.key,
        name=payload.name,
        source_key=payload.source_key,
        event_type=payload.event_type,
        target_type=payload.target_type,
        definition_key=payload.definition_key,
        mapping_json=json.dumps(payload.mapping_json, separators=(",", ":")),
        schedule_kind=payload.schedule_kind,
        interval_seconds=payload.interval_seconds,
        daily_at_utc=payload.daily_at_utc,
        active=True,
        created_at=db.utcnow(),
        updated_at=db.utcnow(),
    )
    session.add(trigger)
    session.flush()
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action="trigger.created",
        status="success",
        ticket_id=None,
        duration_ms=0,
        detail=f"key={payload.key}, target_type={payload.target_type}",
        actor_subject=actor.subject,
    )
    session.commit()
    return TriggerOut(
        key=trigger.key,
        name=trigger.name,
        source_key=trigger.source_key,
        event_type=trigger.event_type,
        target_type=trigger.target_type,
        definition_key=trigger.definition_key,
        mapping_json=json.loads(trigger.mapping_json),
        schedule_kind=trigger.schedule_kind,
        interval_seconds=trigger.interval_seconds,
        daily_at_utc=trigger.daily_at_utc,
        active=trigger.active,
        last_fired_at=trigger.last_fired_at,
        created_at=trigger.created_at,
        updated_at=trigger.updated_at,
    )


@router.get("/triggers/{key}", response_model=TriggerOut)
def get_trigger(
    key: str,
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
) -> TriggerOut:
    trigger = session.get(db.TriggerRecord, key)
    if trigger is None:
        raise HTTPException(status_code=404, detail="Trigger not found.")
    return TriggerOut(
        key=trigger.key,
        name=trigger.name,
        source_key=trigger.source_key,
        event_type=trigger.event_type,
        target_type=trigger.target_type,
        definition_key=trigger.definition_key,
        mapping_json=json.loads(trigger.mapping_json),
        schedule_kind=trigger.schedule_kind,
        interval_seconds=trigger.interval_seconds,
        daily_at_utc=trigger.daily_at_utc,
        active=trigger.active,
        last_fired_at=trigger.last_fired_at,
        created_at=trigger.created_at,
        updated_at=trigger.updated_at,
    )


@router.put("/triggers/{key}", response_model=TriggerOut)
def update_trigger(
    key: str,
    payload: TriggerUpdateIn,
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
) -> TriggerOut:
    trigger = session.get(db.TriggerRecord, key)
    if trigger is None:
        raise HTTPException(status_code=404, detail="Trigger not found.")
    if payload.name is not None:
        trigger.name = payload.name
    if payload.active is not None:
        trigger.active = payload.active
    if payload.mapping_json is not None:
        try:
            triggers.validate_mapping(payload.mapping_json)
        except triggers.MappingError as error:
            raise HTTPException(status_code=422, detail=f"Invalid mapping: {error}") from error
        trigger.mapping_json = json.dumps(payload.mapping_json, separators=(",", ":"))
    trigger.updated_at = db.utcnow()
    session.flush()
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action="trigger.updated",
        status="success",
        ticket_id=None,
        duration_ms=0,
        detail=f"key={key}",
        actor_subject=actor.subject,
    )
    session.commit()
    return TriggerOut(
        key=trigger.key,
        name=trigger.name,
        source_key=trigger.source_key,
        event_type=trigger.event_type,
        target_type=trigger.target_type,
        definition_key=trigger.definition_key,
        mapping_json=json.loads(trigger.mapping_json),
        schedule_kind=trigger.schedule_kind,
        interval_seconds=trigger.interval_seconds,
        daily_at_utc=trigger.daily_at_utc,
        active=trigger.active,
        last_fired_at=trigger.last_fired_at,
        created_at=trigger.created_at,
        updated_at=trigger.updated_at,
    )


@router.delete("/triggers/{key}")
def delete_trigger(
    key: str,
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
) -> dict[str, str]:
    trigger = session.get(db.TriggerRecord, key)
    if trigger is None:
        raise HTTPException(status_code=404, detail="Trigger not found.")
    session.delete(trigger)
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action="trigger.deleted",
        status="success",
        ticket_id=None,
        duration_ms=0,
        detail=f"key={key}",
        actor_subject=actor.subject,
    )
    session.commit()
    return {"status": "deleted"}


@router.get("/triggers/{key}/deliveries", response_model=list[TriggerDeliveryOut])
def list_trigger_deliveries(
    key: str,
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
) -> list[TriggerDeliveryOut]:
    trigger = session.get(db.TriggerRecord, key)
    if trigger is None:
        raise HTTPException(status_code=404, detail="Trigger not found.")
    deliveries = session.execute(
        select(db.TriggerDeliveryRecord)
        .where(db.TriggerDeliveryRecord.trigger_key == key)
        .order_by(db.TriggerDeliveryRecord.id.desc())
        .limit(100)
    ).scalars().all()
    return [
        TriggerDeliveryOut(
            id=d.id,
            trigger_key=d.trigger_key,
            event_id=d.event_id,
            payload=json.loads(d.payload_json),
            status=d.status,
            request_id=d.request_id,
            error=d.error,
            received_at=d.received_at,
            processed_at=d.processed_at,
        )
        for d in deliveries
    ]


@router.post("/triggers/{key}/test")
def test_trigger(
    key: str,
    payload: dict[str, Any],
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
) -> dict[str, Any]:
    """Test a trigger with a synthetic payload (no external calls)."""
    trigger = session.get(db.TriggerRecord, key)
    if trigger is None:
        raise HTTPException(status_code=404, detail="Trigger not found.")
    if not trigger.active:
        raise HTTPException(status_code=409, detail="Trigger is inactive.")

    try:
        mapped = triggers.apply_mapping(json.loads(trigger.mapping_json), payload, trigger)
    except (triggers.MappingError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    return {
        "trigger_key": trigger.key,
        "target_type": trigger.target_type,
        "mapped_payload": mapped,
    }


@router.get("/ui/triggers", response_class=HTMLResponse, include_in_schema=False)
def ui_triggers(
    request: Request,
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
) -> HTMLResponse:
    trigger_records = session.execute(select(db.TriggerRecord)).scalars().all()
    rows = [
        TriggerOut(
            key=t.key,
            name=t.name,
            source_key=t.source_key,
            event_type=t.event_type,
            target_type=t.target_type,
            definition_key=t.definition_key,
            mapping_json=json.loads(t.mapping_json),
            schedule_kind=t.schedule_kind,
            interval_seconds=t.interval_seconds,
            daily_at_utc=t.daily_at_utc,
            active=t.active,
            last_fired_at=t.last_fired_at,
            created_at=t.created_at,
            updated_at=t.updated_at,
        )
        for t in trigger_records
    ]
    return web.render(
        request,
        "triggers.html",
        {"active": "triggers", "triggers": rows},
    )


@router.get("/ui/triggers/{key}", response_class=HTMLResponse, include_in_schema=False)
def ui_trigger_detail(
    key: str,
    request: Request,
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
) -> HTMLResponse:
    trigger = session.get(db.TriggerRecord, key)
    if trigger is None:
        return web.render(request, "404.html", {"active": "triggers"}, status_code=404)
    trigger_out = TriggerOut(
        key=trigger.key,
        name=trigger.name,
        source_key=trigger.source_key,
        event_type=trigger.event_type,
        target_type=trigger.target_type,
        definition_key=trigger.definition_key,
        mapping_json=json.loads(trigger.mapping_json),
        schedule_kind=trigger.schedule_kind,
        interval_seconds=trigger.interval_seconds,
        daily_at_utc=trigger.daily_at_utc,
        active=trigger.active,
        last_fired_at=trigger.last_fired_at,
        created_at=trigger.created_at,
        updated_at=trigger.updated_at,
    )
    deliveries = session.execute(
        select(db.TriggerDeliveryRecord)
        .where(db.TriggerDeliveryRecord.trigger_key == key)
        .order_by(db.TriggerDeliveryRecord.id.desc())
        .limit(20)
    ).scalars().all()
    delivery_rows = [
        TriggerDeliveryOut(
            id=d.id,
            trigger_key=d.trigger_key,
            event_id=d.event_id,
            payload=json.loads(d.payload_json),
            status=d.status,
            request_id=d.request_id,
            error=d.error,
            received_at=d.received_at,
            processed_at=d.processed_at,
        )
        for d in deliveries
    ]
    return web.render(
        request,
        "trigger_detail.html",
        {"active": "triggers", "trigger": trigger_out, "deliveries": delivery_rows},
    )



def trigger_metrics_lines(session) -> list[str]:
    """Prometheus lines for trigger deliveries, derived from the ledger."""
    from collections import Counter

    deliveries = session.execute(select(db.TriggerDeliveryRecord)).scalars().all()
    counts = Counter((row.trigger_key, row.status) for row in deliveries)
    lines = [
        "# HELP flowops_trigger_deliveries_total Trigger deliveries by trigger and status",
        "# TYPE flowops_trigger_deliveries_total counter",
    ]
    for (trigger_key, status), count in sorted(counts.items()):
        lines.append(
            f'flowops_trigger_deliveries_total{{trigger="{trigger_key}",status="{status}"}} {count}'
        )
    triggers_active = session.execute(select(db.TriggerRecord)).scalars().all()
    lines.extend(
        [
            "# HELP flowops_triggers_active Active trigger definitions",
            "# TYPE flowops_triggers_active gauge",
            f"flowops_triggers_active {sum(1 for t in triggers_active if t.active)}",
        ]
    )
    return lines
