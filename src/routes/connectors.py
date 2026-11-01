"""v1.4 connector framework and event-driven trigger management routes.

Admin surface (``/connectors``, ``/trigger-rules``) configures sources and
rules; the operator/webhook surface (``/events``) ingests canonical events,
and the per-connector webhook sink authenticates transport-level signatures
before handing deliveries to the shared ingestion pipeline in
``src.connectors``.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from src import connectors as connector_lib
from src import db
from src.auth import Actor
from src.deps import _admin_actor, _webhook_actor

router = APIRouter(tags=["connectors"])


class ConnectorIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    connector_type: str = Field(pattern=r"^(jira|servicenow|slack|msteams|generic)$")
    tenant_key: Optional[str] = Field(default=None, max_length=64)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ConnectorOut(ConnectorIn):
    id: int
    kind: str
    last_polled_at: Optional[str] = None


class TriggerRuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    tenant_key: Optional[str] = Field(default=None, max_length=64)
    source: Optional[str] = Field(default=None, max_length=40)
    event_type: str = Field(default="*", min_length=1, max_length=80)
    predicate: str = Field(default="true", min_length=1, max_length=2000)
    action_type: str = Field(pattern=r"^(start_workflow|notify)$")
    request_type: Optional[str] = Field(default=None, pattern=r"^(onboarding|offboarding)$")
    notify_url: Optional[str] = Field(default=None, max_length=500)
    static_inputs: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class TriggerRuleOut(TriggerRuleIn):
    id: int
    fired_count: int


class EventIn(BaseModel):
    """One canonical event (the same envelope GenericJsonConnector accepts)."""

    source: str = Field(default="api", min_length=1, max_length=40)
    type: str = Field(min_length=1, max_length=80)
    id: Optional[str] = Field(default=None, max_length=255)
    title: str = Field(default="", max_length=200)
    description: str = Field(default="")
    payload: dict[str, Any] = Field(default_factory=dict)
    tenant: Optional[str] = Field(default=None, max_length=64)


def _to_normalized(item: EventIn) -> connector_lib.NormalizedEvent:
    import uuid

    return connector_lib.NormalizedEvent(
        source=item.source,
        event_type=item.type,
        external_id=item.id or uuid.uuid4().hex,
        payload=item.payload,
        tenant_key=item.tenant,
        title=item.title,
        description=item.description,
    )


def _connector_out(record: db.ConnectorRecord) -> ConnectorOut:
    return ConnectorOut(
        id=record.id,
        name=record.name,
        connector_type=record.connector_type,
        tenant_key=record.tenant_key,
        config=json.loads(record.config_json or "{}"),
        enabled=record.enabled,
        kind=record.kind,
        last_polled_at=record.last_polled_at.isoformat() if record.last_polled_at else None,
    )


def _rule_out(record: db.TriggerRuleRecord) -> TriggerRuleOut:
    try:
        inputs = json.loads(record.static_inputs_json or "{}")
    except json.JSONDecodeError:
        inputs = {}
    return TriggerRuleOut(
        id=record.id,
        name=record.name,
        tenant_key=record.tenant_key,
        source=record.source,
        event_type=record.event_type,
        predicate=record.predicate,
        action_type=record.action_type,
        request_type=record.request_type,
        notify_url=record.notify_url,
        static_inputs=inputs,
        enabled=record.enabled,
        fired_count=record.fired_count,
    )


# ---------------------------------------------------------------------------
# Connectors (admin)
# ---------------------------------------------------------------------------


@router.get("/connectors", response_model=list[ConnectorOut])
def list_connectors(
    actor: Actor = Depends(_admin_actor),
    session: Session = Depends(db.get_session),
) -> list[ConnectorOut]:
    tenant = _actor_tenant(actor)
    query = select(db.ConnectorRecord).order_by(db.ConnectorRecord.id)
    if tenant:
        query = query.where(db.ConnectorRecord.tenant_key == tenant)
    records = session.execute(query).scalars()
    return [_connector_out(r) for r in records]


@router.post(
    "/connectors", response_model=ConnectorOut, status_code=201
)
def create_connector(
    payload: ConnectorIn,
    actor: Actor = Depends(_admin_actor),
    session: Session = Depends(db.get_session),
) -> ConnectorOut:
    actor_tenant = _actor_tenant(actor)
    if actor_tenant:
        if payload.tenant_key and payload.tenant_key != actor_tenant:
            raise HTTPException(404, detail="Tenant mismatch.")
        payload.tenant_key = actor_tenant
    _require_active_tenant(session, payload.tenant_key)
    cls = connector_lib.CONNECTOR_REGISTRY.get(payload.connector_type)
    if cls is None:
        raise HTTPException(422, f"Unknown connector type: {payload.connector_type!r}")
    missing = [k for k in connector_lib.REQUIRED_CONFIG_KEYS[payload.connector_type] if not payload.config.get(k)]
    if missing:
        raise HTTPException(
            422, f"Connector config missing required keys: {missing}"
        )
    for key, value in payload.config.items():
        if not isinstance(value, (str, int, float, bool)) or len(str(value)) > 500:
            raise HTTPException(422, "Connector config values must be short scalars.")
    record = db.ConnectorRecord(
        name=payload.name,
        connector_type=payload.connector_type,
        kind=cls.kind,
        tenant_key=payload.tenant_key,
        config_json=json.dumps(payload.config, separators=(",", ":")),
        enabled=payload.enabled,
    )
    session.add(record)
    session.flush()
    db.log_execution(
        session,
        run_id=record.name,
        action="connector.created",
        status="success",
        detail=f"id={record.id} type={record.connector_type} by={actor.subject}",
    )
    return _connector_out(record)


@router.put("/connectors/{connector_id}", response_model=ConnectorOut)
def update_connector(
    connector_id: int,
    payload: ConnectorIn,
    actor: Actor = Depends(_admin_actor),
    session: Session = Depends(db.get_session),
) -> ConnectorOut:
    record = session.get(db.ConnectorRecord, connector_id)
    if record is None:
        raise HTTPException(404, "Connector not found.")
    _require_connector_access(actor, record)
    actor_tenant = _actor_tenant(actor)
    if actor_tenant:
        if payload.tenant_key and payload.tenant_key != actor_tenant:
            raise HTTPException(404, detail="Tenant mismatch.")
        payload.tenant_key = actor_tenant
    _require_active_tenant(session, payload.tenant_key)
    cls = connector_lib.CONNECTOR_REGISTRY.get(payload.connector_type)
    if cls is None:
        raise HTTPException(422, f"Unknown connector type: {payload.connector_type!r}")
    record.name = payload.name
    record.connector_type = payload.connector_type
    record.kind = cls.kind
    record.tenant_key = payload.tenant_key
    record.config_json = json.dumps(payload.config, separators=(",", ":"))
    record.enabled = payload.enabled
    session.flush()
    return _connector_out(record)


@router.delete("/connectors/{connector_id}")
def delete_connector(
    connector_id: int,
    actor: Actor = Depends(_admin_actor),
    session: Session = Depends(db.get_session),
) -> dict[str, bool]:
    record = session.get(db.ConnectorRecord, connector_id)
    if record is None:
        raise HTTPException(404, "Connector not found.")
    _require_connector_access(actor, record)
    session.delete(record)
    return {"deleted": True}


@router.post("/connectors/poll", status_code=200)
def poll_now(
    actor: Actor = Depends(_admin_actor),
) -> dict[str, int]:
    """Poll every enabled polling connector once, inline."""
    return {"ingested": connector_lib.poll_all_connectors(tenant_key=_actor_tenant(actor))}


@router.post("/connectors/{connector_id}/webhook", status_code=201)
async def connector_webhook(
    connector_id: int,
    request: Request,
    session: Session = Depends(db.get_session),
    actor_subject: str = Depends(_webhook_actor),
) -> dict[str, Any]:
    """Per-connector webhook sink for pushed (webhook-kind) sources.

    Slack deliveries are verified against the signing secret when one is
    configured; other sinks accept the canonical envelope directly.
    """
    import json as _json

    record = session.get(db.ConnectorRecord, connector_id)
    if record is None:
        raise HTTPException(404, "Connector not found.")

    # Delivery-mode check first (kind is stored on the record itself), so a
    # polled connector answers 405 even when its secrets are unavailable.
    registered = connector_lib.CONNECTOR_REGISTRY.get(record.connector_type)
    if registered is None or registered.kind != "webhook":
        raise HTTPException(405, f"Connector '{record.name}' is polled, not pushed.")

    try:
        connector = connector_lib.build_connector(
            record.connector_type, json.loads(record.config_json)
        )
    except ValueError as error:
        # Configured but secret env missing -> service unavailable, not a crash.
        raise HTTPException(503, str(error))

    body = await request.body()
    verifier = getattr(connector, "verify_webhook", None)
    if verifier is not None and not verifier(request, body):
        raise HTTPException(401, "Webhook signature verification failed.")
    try:
        payload = _json.loads(body or b"{}")
    except _json.JSONDecodeError:
        raise HTTPException(400, "Request body is not valid JSON.")

    events = connector.parse_webhook(payload)
    if record.tenant_key:
        events = [
            connector_lib.NormalizedEvent(
                source=event.source,
                event_type=event.event_type,
                external_id=event.external_id,
                payload=event.payload,
                tenant_key=record.tenant_key,
                title=event.title,
                description=event.description,
            )
            for event in events
        ]
    results = connector_lib.ingest_events(session, events)
    return {"received": len(results), "results": results}


# ---------------------------------------------------------------------------
# Trigger rules (admin)
# ---------------------------------------------------------------------------


def _actor_tenant(actor: Actor) -> Optional[str]:
    return None if actor.source == "local" else actor.tenant_key


def _require_rule_access(actor: Actor, record: db.TriggerRuleRecord) -> None:
    tenant = _actor_tenant(actor)
    if tenant and record.tenant_key != tenant:
        raise HTTPException(status_code=404, detail="Trigger rule not found.")


def _require_connector_access(actor: Actor, record: db.ConnectorRecord) -> None:
    tenant = _actor_tenant(actor)
    if tenant and record.tenant_key != tenant:
        raise HTTPException(status_code=404, detail="Connector not found.")


def _require_active_tenant(session: Session, tenant_key: str | None) -> None:
    if tenant_key is None:
        return
    tenant = session.get(db.TenantRecord, tenant_key)
    if tenant is None or not tenant.active:
        raise HTTPException(422, f"Unknown or inactive tenant: {tenant_key}")


@router.get("/trigger-rules", response_model=list[TriggerRuleOut])
def list_trigger_rules(
    actor: Actor = Depends(_admin_actor),
    session: Session = Depends(db.get_session),
) -> list[TriggerRuleOut]:
    tenant = _actor_tenant(actor)
    query = select(db.TriggerRuleRecord).order_by(db.TriggerRuleRecord.id)
    if tenant:
        query = query.where(db.TriggerRuleRecord.tenant_key == tenant)
    records = session.execute(query).scalars()
    return [_rule_out(r) for r in records]


@router.post("/trigger-rules", response_model=TriggerRuleOut, status_code=201)
def create_trigger_rule(
    payload: TriggerRuleIn,
    actor: Actor = Depends(_admin_actor),
    session: Session = Depends(db.get_session),
) -> TriggerRuleOut:
    connector_lib.validate_rule_shape(
        payload.predicate,
        payload.action_type,
        payload.request_type,
        payload.notify_url,
        json.dumps(payload.static_inputs),
    )
    actor_tenant = _actor_tenant(actor)
    if actor_tenant:
        if payload.tenant_key and payload.tenant_key != actor_tenant:
            raise HTTPException(404, detail="Tenant mismatch.")
        payload.tenant_key = actor_tenant
    if payload.tenant_key:
        tenant = session.get(db.TenantRecord, payload.tenant_key)
        if tenant is None or not tenant.active:
            raise HTTPException(422, f"Unknown or inactive tenant: {payload.tenant_key}")
    duplicate = session.execute(
        select(db.TriggerRuleRecord).where(
            db.TriggerRuleRecord.name == payload.name,
            db.TriggerRuleRecord.tenant_key.is_(None)
            if payload.tenant_key is None
            else db.TriggerRuleRecord.tenant_key == payload.tenant_key,
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise HTTPException(409, "A rule with this name already exists for that tenant scope.")
    record = db.TriggerRuleRecord(
        name=payload.name,
        tenant_key=payload.tenant_key,
        source=payload.source,
        event_type=payload.event_type,
        predicate=payload.predicate,
        action_type=payload.action_type,
        request_type=payload.request_type,
        static_inputs_json=json.dumps(payload.static_inputs),
        notify_url=payload.notify_url,
        enabled=payload.enabled,
    )
    session.add(record)
    session.flush()
    db.log_execution(
        session,
        run_id=payload.name,
        action="trigger_rule.created",
        status="success",
        detail=f"id={record.id} action={record.action_type} by={actor.subject}",
    )
    return _rule_out(record)


@router.put("/trigger-rules/{rule_id}", response_model=TriggerRuleOut)
def update_trigger_rule(
    rule_id: int,
    payload: TriggerRuleIn,
    actor: Actor = Depends(_admin_actor),
    session: Session = Depends(db.get_session),
) -> TriggerRuleOut:
    record = session.get(db.TriggerRuleRecord, rule_id)
    if record is None:
        raise HTTPException(404, "Trigger rule not found.")
    _require_rule_access(actor, record)
    actor_tenant = _actor_tenant(actor)
    if actor_tenant:
        if payload.tenant_key and payload.tenant_key != actor_tenant:
            raise HTTPException(404, detail="Tenant mismatch.")
        # PUT is a full replacement, but callers may omit this optional field.
        # Keep the rule scoped to the authenticated tenant so it cannot become
        # a global rule after a tenant-scoped update.
        payload.tenant_key = actor_tenant
    connector_lib.validate_rule_shape(
        payload.predicate,
        payload.action_type,
        payload.request_type,
        payload.notify_url,
        json.dumps(payload.static_inputs),
    )
    record.name = payload.name
    record.tenant_key = payload.tenant_key
    record.source = payload.source
    record.event_type = payload.event_type
    record.predicate = payload.predicate
    record.action_type = payload.action_type
    record.request_type = payload.request_type
    record.static_inputs_json = json.dumps(payload.static_inputs)
    record.notify_url = payload.notify_url
    record.enabled = payload.enabled
    session.flush()
    return _rule_out(record)


@router.delete("/trigger-rules/{rule_id}")
def delete_trigger_rule(
    rule_id: int,
    actor: Actor = Depends(_admin_actor),
    session: Session = Depends(db.get_session),
) -> dict[str, bool]:
    record = session.get(db.TriggerRuleRecord, rule_id)
    if record is None:
        raise HTTPException(404, "Trigger rule not found.")
    _require_rule_access(actor, record)
    session.delete(record)
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Event ingestion
# ---------------------------------------------------------------------------


@router.post("/events", status_code=201)
async def ingest_events_api(
    request: Request,
    actor_subject: str = Depends(_webhook_actor),
    session: Session = Depends(db.get_session),
) -> dict[str, Any]:
    """Ingest one canonical event, a list of them, or an envelope batch.

    Trigger rules fire synchronously; ticket intake is NOT implied here —
    use /webhooks/intake for tickets, /events for signal-driven automation.
    Reads the raw body so both JSON objects and lists are accepted.
    """
    import json as _json

    try:
        payload = _json.loads(await request.body() or b"{}")
    except _json.JSONDecodeError:
        raise HTTPException(400, "Request body is not valid JSON.")
    if isinstance(payload, list):
        payload = {"events": payload}
    items = payload.get("events") if isinstance(payload, dict) else None
    if items is None and isinstance(payload, dict) and "type" in payload:
        items = [payload]
    if items is None:
        raise HTTPException(422, "Provide {'events': [...]} or a single event object.")
    events = [
        _to_normalized(EventIn.model_validate(item)) for item in items[:100]
    ]
    if not events:
        raise HTTPException(422, "No events in payload.")
    summaries = connector_lib.ingest_events(session, events)
    return {
        "received": len(events),
        "results": summaries,
    }
