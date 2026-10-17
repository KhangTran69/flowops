"""Tenant registry, cross-tenant drift checks, and PSA (ConnectWise) sync."""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src import connectwise, db, tenants
from src.auth import Actor
from src.deps import _admin_actor, _operator_actor

router = APIRouter(tags=["tenants"])


class TenantIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    entra_tenant_id: Optional[str] = Field(default=None, max_length=64)
    default_domain: Optional[str] = Field(default=None, max_length=255)
    licence_sku: Optional[str] = Field(default=None, max_length=120)
    usage_location: Optional[str] = Field(default=None, pattern=r"^[A-Z]{2}$")
    psa_company_id: Optional[str] = Field(default=None, pattern=r"^\d+$")
    credential_ref: Optional[str] = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_]{1,64}$",
        description="Suffix of FLOWOPS_TENANT_<REF>_GRAPH_CLIENT_ID/SECRET env vars. Never the secret itself.",
    )
    baseline: dict[str, Any] = Field(default_factory=dict)
    active: bool = True


class TenantOut(TenantIn):
    key: str


def _out(record: db.TenantRecord) -> TenantOut:
    settings = tenants.TenantSettings.from_record(record)
    return TenantOut(
        key=settings.key,
        name=settings.name,
        entra_tenant_id=settings.entra_tenant_id,
        default_domain=settings.default_domain,
        licence_sku=settings.licence_sku,
        usage_location=settings.usage_location,
        psa_company_id=settings.psa_company_id,
        credential_ref=settings.credential_ref,
        baseline=settings.baseline,
        active=settings.active,
    )


@router.get("/tenants", response_model=list[TenantOut])
def list_tenants(session: Session = Depends(db.get_session)) -> list[TenantOut]:
    return [_out(r) for r in session.query(db.TenantRecord).order_by(db.TenantRecord.key)]


@router.get("/tenants/{key}", response_model=TenantOut)
def get_tenant(key: str, session: Session = Depends(db.get_session)) -> TenantOut:
    record = session.get(db.TenantRecord, key)
    if record is None:
        raise HTTPException(404, "Tenant not found.")
    return _out(record)


@router.put("/tenants/{key}", response_model=TenantOut)
def upsert_tenant(
    key: str,
    body: TenantIn,
    actor: Actor = Depends(_admin_actor),
    session: Session = Depends(db.get_session),
) -> TenantOut:
    import re

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", key):
        raise HTTPException(422, "Tenant key must be lowercase letters, digits and dashes.")
    record = session.get(db.TenantRecord, key) or db.TenantRecord(key=key)
    for field in ("name", "entra_tenant_id", "default_domain", "licence_sku", "usage_location",
                  "psa_company_id", "credential_ref", "active"):
        setattr(record, field, getattr(body, field))
    record.baseline_json = json.dumps(body.baseline, sort_keys=True)
    session.add(record)
    session.flush()
    db.log_execution(
        session, run_id=uuid.uuid4().hex[:12], action="tenant.upserted", status="success",
        detail=f"tenant {key}", actor_subject=actor.subject,
    )
    return _out(record)


def graph_factory() -> tenants.GraphClientFactory:
    adapter = os.environ.get("FLOWOPS_ACTION_ADAPTER", "simulated").lower()
    return tenants.GraphClientFactory(simulated=adapter != "graph")


@router.post("/tenants/drift-check")
def drift_check(
    tenant: Optional[str] = Query(default=None, description="Check one tenant; default all active."),
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> dict[str, Any]:
    """Compare each tenant's licences/security defaults with its baseline."""
    selected = tenants.list_tenants(session, active_only=True)
    if tenant:
        selected = [t for t in selected if t.key == tenant]
        if not selected:
            raise HTTPException(404, "Tenant not found or inactive.")
    run_id = uuid.uuid4().hex[:12]
    started = time.perf_counter()
    results = tenants.run_drift_check(selected, graph_factory())
    for result in results:
        db.log_execution(
            session, run_id=run_id, action="tenant.drift_check",
            status="failure" if result["status"] == "error" else "success",
            duration_ms=int((time.perf_counter() - started) * 1000),
            detail=f"[{result['tenant']}] {result['status']} drift={result['drift_count']}"
            + (f" error={result['error']}" if result["error"] else ""),
            actor_subject=actor.subject,
        )
    return {
        "run_id": run_id,
        "checked": len(results),
        "drifted": sum(1 for r in results if r["status"] == "drift"),
        "errors": sum(1 for r in results if r["status"] == "error"),
        "results": results,
    }


@router.post("/tickets/{ticket_id}/psa-sync")
def psa_sync(
    ticket_id: str,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> dict[str, Any]:
    """Create/update the mirrored ConnectWise Manage ticket (idempotent)."""
    ticket = session.get(db.TicketRecord, ticket_id)
    if ticket is None:
        raise HTTPException(404, "Ticket not found.")
    try:
        client = connectwise.client_from_environment()
    except ValueError as error:
        raise HTTPException(503, f"ConnectWise is not configured: {error}") from error
    try:
        result = connectwise.sync_ticket(session, ticket, client)
    except connectwise.ConnectWiseError as error:
        status = 422 if error.status_code == 422 else 502
        raise HTTPException(status, str(error)) from error
    db.log_execution(
        session, run_id=uuid.uuid4().hex[:12], action=f"psa.{result['action']}", status="success",
        ticket_id=ticket_id, detail=f"ConnectWise ticket {result['connectwise_id']}",
        actor_subject=actor.subject,
    )
    return result
