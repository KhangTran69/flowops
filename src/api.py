"""FlowOps API — ticket intake, routing, onboarding workflows, and monitoring.

v0.3 adds database persistence and execution logs; v0.4 adds rule-based
routing, outbound notifications, and an n8n-friendly webhook; v0.5 adds
onboarding/offboarding checklists, idempotency keys, and delivery retries;
the v1.0 preview keeps failed webhook deliveries in a retryable outbox.
"""

import hashlib
import html
import json
import os
import time
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi import status as http_status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool, StrictInt, StrictStr, field_validator
from sqlalchemy import and_, delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from src import db
from src.actions import ACTION_BY_STEP, ActionResult, action_for_step, adapter_from_environment
from src.m365_simulator import M365Simulator
from src.notify import deliver_webhook
from src.onboarding import checklist_for
from src.routing import route_ticket
from src.ticket_processor import normalize_ticket

ACTION_ADAPTER: ActionAdapter = adapter_from_environment()
DEFAULT_ACTION_ATTEMPTS = 3
DEFAULT_NOTIFICATION_MAX_ATTEMPTS = 5
IDEMPOTENCY_PROCESSING_TIMEOUT = timedelta(minutes=5)
IDEMPOTENCY_SCOPE_TICKET = "ticket"
IDEMPOTENCY_SCOPE_ONBOARDING = "onboarding"
IDEMPOTENCY_REPLAY_ACTIONS = {
    IDEMPOTENCY_SCOPE_TICKET: "ticket.idempotent_replay",
    IDEMPOTENCY_SCOPE_ONBOARDING: "onboarding.request.idempotent_replay",
}


def _action_attempts(action: str) -> int:
    """Read a bounded per-action retry count from JSON configuration."""
    try:
        configured = json.loads(os.environ.get("FLOWOPS_ACTION_RETRIES", "{}"))
        return max(1, min(10, int(configured.get(action, DEFAULT_ACTION_ATTEMPTS))))
    except (TypeError, ValueError, json.JSONDecodeError):
        return DEFAULT_ACTION_ATTEMPTS


def _notification_max_attempts() -> int:
    """Read the bounded dead-letter threshold for outbox deliveries."""
    try:
        configured = int(
            os.environ.get(
                "FLOWOPS_NOTIFICATION_MAX_ATTEMPTS",
                DEFAULT_NOTIFICATION_MAX_ATTEMPTS,
            )
        )
        return max(1, min(20, configured))
    except (TypeError, ValueError):
        return DEFAULT_NOTIFICATION_MAX_ATTEMPTS


def _notify_action_failure(
    action: str, context: dict[str, Any], attempts: int, error: str, session: Session
) -> None:
    """Notify an optional failure webhook without raising."""
    url = os.environ.get("FLOWOPS_ACTION_FAILURE_WEBHOOK_URL")
    if not url:
        return
    delivery = deliver_webhook(
        url,
        {"event": "onboarding.action.failed", "action": action, "context": context, "attempts": attempts, "error": error},
    )
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action="notification.action_failure",
        status="success" if delivery.success else "failure",
        duration_ms=0,
        detail=f"attempts={delivery.attempts}; {delivery.last_error or 'delivered'}",
    )


# v0.6 action boundary: the adapter that executes checklist actions. Tests
# monkeypatch this module attribute with failing/succeeding doubles.
ACTION_ADAPTER = adapter_from_environment()

app = FastAPI(
    title="FlowOps",
    description=(
        "IT workflow automation platform — ticket intake, routing, "
        "onboarding workflows, and monitoring. Learning prototype; "
        "ticket data is simulated, not real support tickets."
    ),
    version="1.0.0-preview",
)
app.mount(
    "/static",
    StaticFiles(directory=str(web.BASE_DIR / "static")),
    name="static",
)


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


# Recovery lease for in-flight idempotency reservations (README: abandoned
# requests can be reclaimed after five minutes). Used by the claim/takeover
# logic below and by _reservation_is_stale().
IDEMPOTENCY_PROCESSING_TIMEOUT = timedelta(minutes=5)


def _claim_idempotency_key(
    session: Session,
    key: str,
    resource_id: str,
    request_hash: str,
    resource_type: str,
) -> Optional[str]:
    """Atomically claim a key or take over an expired processing reservation.

    Returns a fresh fencing token when this request owns the reservation, or
    ``None`` when another request owns it. A takeover replaces the token and
    refreshes the lease timestamp in the same upsert, so the previous owner
    loses the ability to finalize or remove the reservation.
    """
    table = db.IdempotencyRecord.__table__
    now = db.utcnow()
    stale_before = now - IDEMPOTENCY_PROCESSING_TIMEOUT
    owner_token = uuid.uuid4().hex
    values = {
        "key": key,
        "ticket_id": resource_id,
        "response_json": json.dumps(
            {
                "request_hash": request_hash,
                "state": "processing",
                "resource_type": resource_type,
            },
            separators=(",", ":"),
        ),
        "owner_token": owner_token,
        "resource_type": resource_type,
        "created_at": now,
    }
    scope_matches = table.c.resource_type == resource_type
    if resource_type == IDEMPOTENCY_SCOPE_TICKET:
        # Reservations created before scoping existed are ticket reservations.
        scope_matches = or_(scope_matches, table.c.resource_type.is_(None))
    takeover = and_(
        table.c.created_at < stale_before,
        table.c.response_json.like('%"state":"processing"%'),
        scope_matches,
    )
    set_values = {
        "ticket_id": resource_id,
        "response_json": values["response_json"],
        "owner_token": owner_token,
        "resource_type": resource_type,
        "created_at": now,
    }
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        statement = sqlite_insert(table).values(**values).on_conflict_do_update(
            index_elements=[table.c.key],
            set_=set_values,
            where=takeover,
        )
        # SQLite reports 1 for both the insert and the takeover update.
        claimed = session.execute(statement).rowcount == 1
    elif dialect == "postgresql":
        statement = (
            postgres_insert(table)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[table.c.key],
                set_=set_values,
                where=takeover,
            )
            .returning(table.c.owner_token)
        )
        # psycopg reports -1 for this upsert, so confirm the claim through the
        # RETURNING row instead of relying on a driver-specific rowcount.
        claimed = session.execute(statement).scalar_one_or_none() == owner_token
    else:
        raise RuntimeError(f"Idempotency reservations do not support database dialect {dialect!r}.")

    session.commit()
    return owner_token if claimed else None


def _complete_idempotency_key(
    session: Session,
    key: str,
    owner_token: str,
    request_hash: str,
    resource_type: str,
    resource_id: str,
    completion: dict[str, Any],
) -> bool:
    """Store the completed response only if this request still owns the key.

    The conditional update runs in the same transaction as the resource and
    execution-log writes, so losing the fencing token rolls all of them back
    instead of committing a duplicate durable result.
    """
    table = db.IdempotencyRecord.__table__
    payload = {
        "request_hash": request_hash,
        "state": "completed",
        "resource_type": resource_type,
        **completion,
    }
    result = session.execute(
        update(table)
        .where(
            and_(
                table.c.key == key,
                table.c.owner_token == owner_token,
                table.c.resource_type == resource_type,
                table.c.response_json.like('%"state":"processing"%'),
            )
        )
        .values(
            ticket_id=resource_id,
            response_json=json.dumps(payload, separators=(",", ":")),
        )
    )
    return result.rowcount == 1


def _release_idempotency_key(
    session: Session, key: str, owner_token: str
) -> bool:
    """Delete a failed request's reservation, but only while it still owns it."""
    table = db.IdempotencyRecord.__table__
    result = session.execute(
        delete(table).where(
            and_(
                table.c.key == key,
                table.c.owner_token == owner_token,
                table.c.response_json.like('%"state":"processing"%'),
            )
        )
    )
    released = result.rowcount == 1
    if released:
        session.commit()
    else:
        session.rollback()
    return released


def _reservation_is_stale(created_at: datetime) -> bool:
    """Return whether a processing reservation has exceeded its recovery lease."""
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at < db.utcnow() - IDEMPOTENCY_PROCESSING_TIMEOUT


def _replay_stored_response(
    stored: db.IdempotencyRecord,
    payload: dict[str, Any],
    request_hash: str,
    resource_type: str,
    response: Response,
    session: Session,
) -> dict[str, Any]:
    """Validate and replay a stored completed response, or return a 409."""
    if _stored_resource_type(payload) != resource_type:
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key was already used for a different resource type.",
        )
    if payload.get("request_hash") != request_hash:
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key was already used with a different request.",
        )
    if payload.get("state") == "processing":
        raise HTTPException(
            status_code=409,
            detail="A request with this Idempotency-Key is still processing.",
        )
    response.status_code = http_status.HTTP_200_OK
    response.headers["Idempotency-Replayed"] = "true"
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action=IDEMPOTENCY_REPLAY_ACTIONS[resource_type],
        status="success",
        ticket_id=stored.ticket_id,
        detail="replayed stored response without re-executing",
    )
    return payload


def _reserve_idempotency_key(
    session: Session,
    response: Response,
    key: Optional[str],
    request_hash: str,
    resource_type: str,
    resource_id: str,
) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """Claim a reservation or return a completed stored response.

    Returns ``(owner_token, None)`` for a new claim and ``(None, payload)`` for
    a replay. Active, changed-payload, and cross-resource requests raise 409.
    """
    if not key:
        return None, None
    stored = session.get(db.IdempotencyRecord, key)
    if stored is not None:
        payload = json.loads(stored.response_json)
        if _stored_resource_type(payload) != resource_type:
            raise HTTPException(
                status_code=409,
                detail="Idempotency-Key was already used for a different resource type.",
            )
        if payload.get("state") == "processing":
            if payload.get("request_hash") != request_hash:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency-Key was already used with a different request.",
                )
            if not _reservation_is_stale(stored.created_at):
                return None, _replay_stored_response(
                    stored, payload, request_hash, resource_type, response, session
                )
            owner_token = _claim_idempotency_key(
                session, key, resource_id, request_hash, resource_type
            )
        else:
            return None, _replay_stored_response(
                stored, payload, request_hash, resource_type, response, session
            )
    else:
        owner_token = _claim_idempotency_key(
            session, key, resource_id, request_hash, resource_type
        )
    if owner_token is None:
        session.expire_all()
        stored = session.get(db.IdempotencyRecord, key)
        if stored is None:
            raise HTTPException(
                409, "Unable to claim this Idempotency-Key; retry the request."
            )
        payload = json.loads(stored.response_json)
        return None, _replay_stored_response(
            stored, payload, request_hash, resource_type, response, session
        )
    return owner_token, None


# ---------------------------------------------------------------------------
# Schemas


class TicketIntakeRequest(BaseModel):
    """Incoming ticket in the source format (same schema as the sample API)."""

    userId: StrictInt = Field(ge=1, description="ID of the requester.")
    id: StrictInt = Field(ge=1, description="Upstream ticket ID.")
    title: StrictStr = Field(description="Short ticket description.")
    completed: StrictBool = Field(description="Whether the upstream item is done.")

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must be a non-empty string")
        return value


class Ticket(BaseModel):
    """Normalized internal ticket format (also the API response shape)."""

    ticket_id: str
    requester_id: int
    title: str
    status: Literal["open", "closed"]
    priority: str
    category: Optional[str] = None
    queue: Optional[str] = None


class ExecutionLogOut(BaseModel):
    id: int
    run_id: str
    action: str
    status: str
    ticket_id: Optional[str] = None
    duration_ms: int
    detail: Optional[str] = None
    created_at: datetime


class ActionExecutionOut(BaseModel):
    id: int
    request_id: int
    checklist_item_id: int
    action: str
    actor_subject: Optional[str] = None
    adapter: str
    correlation_id: str
    attempt: int
    status: str
    error_category: Optional[str] = None
    error_detail: Optional[str] = None
    external_reference: Optional[str] = None
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
class ChecklistItemOut(BaseModel):
    id: int
    step: str
    # v0.6.1: stable identifier for the step; None on rows created before
    # this change existed (they predate the offboarding completion ledger).
    step_key: Optional[str] = None
    queue: str
    requires_approval: bool
    status: str




class OnboardingRequestIn(BaseModel):
    """A manager's onboarding/offboarding request (v0.5)."""

    request_type: Literal["onboarding", "offboarding"]
    employee_name: StrictStr
    employee_email: StrictStr
    department: StrictStr
    requester_id: StrictInt = Field(ge=1)
    start_date: Optional[StrictStr] = Field(
        default=None, description="ISO date (YYYY-MM-DD) the employee starts or leaves."
    )

    @field_validator("employee_name", "department")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-empty string")
        return value

    @field_validator("employee_email")
    @classmethod
    def must_look_like_email(cls, value: str) -> str:
        if "@" not in value or " " in value:
            raise ValueError("employee_email must be a valid email address")
        return value.lower()

    @field_validator("start_date")
    @classmethod
    def iso_date(cls, value: Optional[str]) -> Optional[str]:
        import re

        if value is not None:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("start_date must be an ISO date (YYYY-MM-DD)")
            try:
                date.fromisoformat(value)
            except ValueError as error:
                raise ValueError("start_date must be a valid calendar date") from error
        return value


class OnboardingRequestOut(BaseModel):
    id: int
    request_type: str
    employee_name: str
    employee_email: str
    department: str
    requester_id: int
    start_date: Optional[str] = None
    status: str
    checklist: list[ChecklistItemOut]


class OnboardingStatusSummaryOut(BaseModel):
    id: int
    request_type: str
    employee_name: str
    status: str
    total_steps: int
    completed_steps: int
    pending_approval_steps: int
    percent_complete: int


# ---------------------------------------------------------------------------
# Ticket pipeline


def _notify(event: str, ticket: Ticket, run_id: str, session: Session) -> None:
    """Deliver the outbound webhook if configured; never raises.

    A delivery that fails after all in-request retries is stored in the
    notification outbox so it stays visible on the dashboard and can be
    retried later via ``POST /notifications/retry``.
    """
    webhook_url = os.environ.get("FLOWOPS_NOTIFICATION_WEBHOOK_URL")
    if not webhook_url:
        return
    payload = {"event": event, "ticket": ticket.model_dump()}
    started = time.perf_counter()
    delivery = deliver_webhook(webhook_url, payload)
    duration_ms = int((time.perf_counter() - started) * 1000)
    detail = f"attempts={delivery.attempts}"
    if delivery.last_error:
        detail += f"; {delivery.last_error}"
    db.log_execution(
        session,
        run_id=run_id,
        action="notification.webhook",
        status="success" if delivery.success else "failure",
        ticket_id=ticket.ticket_id,
        duration_ms=duration_ms,
        detail=detail,
    )
    if not delivery.success:
        session.add(
            db.NotificationOutboxRecord(
                url=webhook_url,
                payload_json=json.dumps(payload, separators=(",", ":")),
                ticket_id=ticket.ticket_id,
                status="pending",
                attempts=delivery.attempts,
                last_error=delivery.last_error,
            )
        )


def _action_attempts(action: str) -> int:
    """Retry budget for one action: env override JSON, else 3 attempts."""
    overrides = os.environ.get("FLOWOPS_ACTION_RETRIES")
    if overrides:
        try:
            limit = int(json.loads(overrides).get(action, 3))
        except (ValueError, TypeError):
            limit = 3
        return max(1, limit)
    return 3


def _notify_action_failure(
    action: str,
    context: dict,
    attempts: int,
    last_error: str,
    session: Session,
) -> None:
    """Alert operators (never raising) when an action exhausts its retries."""
    webhook_url = os.environ.get("FLOWOPS_ACTION_FAILURE_WEBHOOK_URL")
    if not webhook_url:
        return
    payload = {
        "event": "action.failure",
        "action": action,
        "attempts": attempts,
        "error": last_error,
        "request_id": context.get("request_id"),
        "employee_email": context.get("employee_email"),
    }
    delivery = deliver_webhook(webhook_url, payload)
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action="notification.action_failure",
        status="success" if delivery.success else "failure",
        duration_ms=0,
        detail=f"action '{action}' alert delivered={delivery.success}",
    )


def _process_intake(request: TicketIntakeRequest, session: Session) -> Ticket:
    """Normalize, route, persist, and log one incoming ticket."""
    run_id = _new_run_id()
    started = time.perf_counter()

    ticket = normalize_ticket(request.model_dump())
    decision = route_ticket(ticket["title"])
    ticket["priority"] = decision.priority
    ticket["category"] = decision.category
    ticket["queue"] = decision.queue

    record = session.get(db.TicketRecord, ticket["ticket_id"])
    created = record is None
    if created:
        session.add(db.TicketRecord(**ticket))
    else:
        for field, value in ticket.items():
            setattr(record, field, value)
    session.flush()

    action = "ticket.created" if created else "ticket.updated"
    db.log_execution(
        session,
        run_id=run_id,
        action=action,
        status="success",
        ticket_id=ticket["ticket_id"],
        duration_ms=int((time.perf_counter() - started) * 1000),
        detail=f"routed to {decision.queue} ({decision.category}/{decision.priority})",
    )
    result = Ticket(**ticket)
    _notify(action, result, run_id, session)
    return result


# ---------------------------------------------------------------------------
# Routes: service


def _site_stats(session: Session) -> dict[str, int]:
    """Small aggregate snapshot used by the landing page."""
    tickets = session.execute(select(db.TicketRecord)).scalars().all()
    outbox = session.execute(select(db.NotificationOutboxRecord)).scalars().all()
    onboarding = session.execute(select(db.OnboardingRequestRecord)).scalars().all()
    return {
        "tickets": len(tickets),
        "open_tickets": sum(1 for ticket in tickets if ticket.status == "open"),
        "auto_routed": sum(
            1 for ticket in tickets if ticket.queue and ticket.queue != "it-service-desk"
        ),
        "onboarding_requests": len(onboarding),
        "pending_notifications": sum(1 for item in outbox if item.status == "pending"),
        "failed_notifications": sum(1 for item in outbox if item.status == "failed"),
    }


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def root(
    request: Request, session: Session = Depends(db.get_session)
) -> HTMLResponse:
    """Landing page with live counts and operator links."""
    return web.render(
        request,
        "landing.html",
        {"active": "home", "stats": _site_stats(session)},
    )


@app.get("/health")
def health() -> dict:
    """Liveness probe."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Routes: tickets


@app.post("/tickets", response_model=Ticket, status_code=http_status.HTTP_201_CREATED)
def create_ticket(
    request: TicketIntakeRequest,
    response: Response,
    session: Session = Depends(db.get_session),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> Ticket:
    """Validate, route, and store a ticket.

    Returns 201 for a new ticket, 200 for an update of an existing ID, and
    200 with an Idempotency-Replayed header when the same Idempotency-Key
    is replayed (without re-executing the request).
    """
    _validate_idempotency_key(idempotency_key)
    request_hash = _request_fingerprint(request)
    owner_token, replay = _reserve_idempotency_key(
        session,
        response,
        idempotency_key,
        request_hash,
        IDEMPOTENCY_SCOPE_TICKET,
        str(request.id),
    )
    if replay is not None:
        return Ticket.model_validate(replay["ticket"])

    ownership_lost = False
    try:
        if session.get(db.TicketRecord, str(request.id)) is not None:
            response.status_code = http_status.HTTP_200_OK
        result = _process_intake(request, session)
        if idempotency_key and owner_token is not None:
            if not _complete_idempotency_key(
                session,
                idempotency_key,
                owner_token,
                request_hash,
                IDEMPOTENCY_SCOPE_TICKET,
                result.ticket_id,
                {"ticket": result.model_dump()},
            ):
                # Another request took over the expired lease. Roll back this
                # request's ticket/log writes before reporting a retryable conflict.
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


@app.get("/tickets", response_model=list[Ticket])
def list_tickets(session: Session = Depends(db.get_session)) -> list[Ticket]:
    """List all stored tickets, ordered by numeric ticket ID."""
    records = session.execute(select(db.TicketRecord)).scalars().all()
    tickets = [Ticket.model_validate(record, from_attributes=True) for record in records]
    return sorted(tickets, key=lambda ticket: int(ticket.ticket_id))


@app.get("/tickets/{ticket_id}", response_model=Ticket)
def get_ticket(
    ticket_id: str, session: Session = Depends(db.get_session)
) -> Ticket:
    """Return one stored ticket."""
    if not ticket_id.isdigit():
        raise HTTPException(status_code=404, detail="Ticket not found.")
    record = session.get(db.TicketRecord, ticket_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    return Ticket.model_validate(record, from_attributes=True)


@app.post("/webhooks/intake")
def webhook_intake(
    payload: TicketIntakeRequest | list[TicketIntakeRequest],
    session: Session = Depends(db.get_session),
) -> Any:
    """n8n-friendly intake: accepts one ticket or a list, always 200.

    Each item is validated, routed, stored, and logged exactly like
    POST /tickets. Invalid payloads are rejected with 422 before any
    item is processed.
    """
    if isinstance(payload, list):
        return [_process_intake(item, session) for item in payload]
    return _process_intake(payload, session)


# ---------------------------------------------------------------------------
# Routes: onboarding/offboarding workflows


def _request_out(
    record: db.OnboardingRequestRecord, items: list[db.ChecklistItemRecord]
) -> OnboardingRequestOut:
    return OnboardingRequestOut(
        **{c: getattr(record, c) for c in (
            "id", "request_type", "employee_name", "employee_email",
            "department", "requester_id", "start_date", "status",
        )},
        checklist=[
            ChecklistItemOut(
                **{c: getattr(item, c) for c in (
                    "id", "step", "step_key", "queue", "requires_approval",
                    "status", "action",
                )},
                result=json.loads(item.result) if item.result else None,
            )
            for item in sorted(items, key=lambda i: i.id)
        ],
    )


def _load_request(
    session: Session, request_id: int
) -> tuple[db.OnboardingRequestRecord, list[db.ChecklistItemRecord]]:
    record = session.get(db.OnboardingRequestRecord, request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Onboarding request not found.")
    items = session.execute(
        select(db.ChecklistItemRecord).where(db.ChecklistItemRecord.request_id == request_id)
    ).scalars().all()
    return record, list(items)


def _create_onboarding_request(
    request: OnboardingRequestIn, session: Session
) -> OnboardingRequestOut:
    """Create the record and checklist, returning the API response shape."""
    started = time.perf_counter()
    record = db.OnboardingRequestRecord(
        request_type=request.request_type,
        employee_name=request.employee_name,
        employee_email=request.employee_email,
        department=request.department,
        requester_id=request.requester_id,
        start_date=request.start_date,
    )
    session.add(record)
    session.flush()
    steps = checklist_for(request.request_type)
    for template in steps:
        # The template's ``key`` becomes the row's ``step_key`` column.
        session.add(
            db.ChecklistItemRecord(
                request_id=record.id, step_key=template["key"],
                **{k: v for k, v in template.items() if k != "key"},
            )
        )
    session.flush()
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action="onboarding.request.created",
        status="success",
        duration_ms=int((time.perf_counter() - started) * 1000),
        detail=f"{request.request_type} for {request.employee_name}: "
               f"{len(steps)} checklist steps generated",
    )
    _, items = _load_request(session, record.id)
    return _request_out(record, items)


@app.post(
    "/onboarding-requests",
    response_model=OnboardingRequestOut,
    status_code=http_status.HTTP_201_CREATED,
)
def create_onboarding_request(
    request: OnboardingRequestIn,
    response: Response,
    session: Session = Depends(db.get_session),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> OnboardingRequestOut:
    """Create an onboarding/offboarding request and generate its checklist.

    Supports ``Idempotency-Key`` with the same fenced reservation protocol as
    ``POST /tickets``: replays are 200, changed payloads are 409, and a stale
    owner that loses its lease cannot finalize the takeover's reservation.
    """
    _validate_idempotency_key(idempotency_key)
    request_hash = _request_fingerprint(request)
    owner_token, replay = _reserve_idempotency_key(
        session,
        response,
        idempotency_key,
        request_hash,
        IDEMPOTENCY_SCOPE_ONBOARDING,
        "0",
    )
    if replay is not None:
        return OnboardingRequestOut.model_validate(replay["onboarding_request"])

    ownership_lost = False
    try:
        result = _create_onboarding_request(request, session)
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


@app.get("/onboarding-requests", response_model=list[OnboardingRequestOut])
def list_onboarding_requests(
    session: Session = Depends(db.get_session),
) -> list[OnboardingRequestOut]:
    """List onboarding/offboarding requests with their checklists."""
    records = session.execute(select(db.OnboardingRequestRecord)).scalars().all()
    results = []
    for record in records:
        _, items = _load_request(session, record.id)
        results.append(_request_out(record, items))
    return results


@app.get("/onboarding-requests/{request_id}", response_model=OnboardingRequestOut)
def get_onboarding_request(
    request_id: int, session: Session = Depends(db.get_session)
) -> OnboardingRequestOut:
    record, items = _load_request(session, request_id)
    return _request_out(record, items)


@app.get(
    "/onboarding-requests/{request_id}/status",
    response_model=OnboardingStatusSummaryOut,
)
def get_onboarding_request_status(
    request_id: int, session: Session = Depends(db.get_session)
) -> OnboardingStatusSummaryOut:
    """Return a lightweight summary of request progress for automation polling."""
    record, items = _load_request(session, request_id)
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



@app.get(
    "/ui/onboarding-requests",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def ui_onboarding_requests(
    request: Request, session: Session = Depends(db.get_session)
) -> HTMLResponse:
    """Operator list of onboarding/offboarding requests."""
    records = session.execute(select(db.OnboardingRequestRecord)).scalars().all()
    rows = []
    for record in records:
        _, items = _load_request(session, record.id)
        total = len(items)
        completed = sum(1 for item in items if item.status == "completed")
        rows.append(
            {
                "id": record.id,
                "request_type": record.request_type,
                "employee_name": record.employee_name,
                "department": record.department,
                "status": record.status,
                "completed": completed,
                "total": total,
                "percent": round((completed / total) * 100) if total else 0,
            }
        )
    return web.render(
        request,
        "onboarding_list.html",
        {"active": "onboarding", "requests": rows},
    )


@app.get(
    "/ui/onboarding-requests/{request_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def ui_onboarding_request(
    request: Request,
    request_id: int,
    session: Session = Depends(db.get_session),
) -> HTMLResponse:
    """Operator detail page for one checklist."""
    try:
        record, items = _load_request(session, request_id)
    except HTTPException as error:
        if error.status_code == 404:
            return web.render(
                request,
                "404.html",
                {"active": "onboarding"},
                status_code=404,
            )
        raise
    completed = sum(1 for item in items if item.status == "completed")
    return web.render(
        request,
        "onboarding_detail.html",
        {
            "active": "onboarding",
            "request_record": record,
            "items": sorted(items, key=lambda item: item.id),
            "completed": completed,
            "total": len(items),
        },
    )


@app.get("/ui/notifications", response_class=HTMLResponse, include_in_schema=False)
def ui_notifications(
    request: Request, session: Session = Depends(db.get_session)
) -> HTMLResponse:
    """Operator page for pending and dead-letter webhook deliveries."""
    records = session.execute(
        select(db.NotificationOutboxRecord).order_by(db.NotificationOutboxRecord.id)
    ).scalars().all()
    return web.render(
        request,
        "notifications.html",
        {
            "active": "notifications",
            "pending": [item for item in records if item.status == "pending"],
            "failed": [item for item in records if item.status == "failed"],
        },
    )


@app.get("/ui/workflow-jobs", response_class=HTMLResponse, include_in_schema=False)
def ui_workflow_jobs(
    request: Request, session: Session = Depends(db.get_session)
) -> HTMLResponse:
    """Operator page for the durable workflow queue."""
    return web.render(
        request,
        "jobs.html",
        {"active": "jobs", "jobs": []},
    )


@app.get("/ui/{rest:path}", response_class=HTMLResponse, include_in_schema=False)
def ui_not_found(request: Request, rest: str) -> HTMLResponse:
    """Friendly HTML 404 for unknown operator pages."""
    return web.render(request, "404.html", {"active": ""}, status_code=404)


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
    action: Literal["approve", "complete"],
) -> OnboardingRequestOut:
    record, _ = _load_request(session, request_id)
    item = _get_item(session, record, item_id)

    if action == "approve":
        if not item.requires_approval:
            raise HTTPException(409, "This checklist item does not require approval.")
        if item.status != "pending":
            raise HTTPException(409, f"Cannot approve an item with status '{item.status}'.")
        item.status = "approved"
    else:
        allowed_from = "approved" if item.requires_approval else "pending"
        if item.status != allowed_from:
            raise HTTPException(
                409,
                f"Item must be '{allowed_from}' before completion "
                f"(current status: '{item.status}').",
            )
        item.status = "completed"
    session.flush()

    db.log_execution(
        session,
        run_id=_new_run_id(),
        action=f"onboarding.item.{action}d",
        status="success",
        duration_ms=0,
        detail=f"step '{item.step}' of request #{record.id} is now '{item.status}'",
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
        )
    _, items = _load_request(session, record.id)
    return _request_out(record, items)




def _execute_item(
    session: Session, request_id: int, item_id: int, actor: Actor
) -> OnboardingRequestOut:
    """Run one approved checklist action with bounded retries."""
    record, _ = _load_request(session, request_id)
    item = _get_item(session, record, item_id)
    required_status = "approved" if item.requires_approval else "pending"
    if item.status != required_status:
        raise HTTPException(
            409,
            f"Item must be '{required_status}' before execution "
            f"(current status: '{item.status}').",
        )

    action = action_for_step(item.step)
    context = {
        "request_id": record.id,
        "request_type": record.request_type,
        "employee_name": record.employee_name,
        "employee_email": record.employee_email,
        "department": record.department,
        "actor_subject": actor.subject,
        "actor_roles": list(actor.roles),
    }
    correlation_id = _new_run_id()
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
                adapter=ACTION_ADAPTER.__class__.__name__,
                correlation_id=correlation_id,
                attempt=attempt,
                status="succeeded",
                external_reference=result.detail[:256],
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=int((time.perf_counter() - started) * 1000),
            ))
            item.status = "completed"
            session.flush()
            db.log_execution(
                session,
                run_id=_new_run_id(),
                action=f"onboarding.action.{action}",
                status="success",
                duration_ms=int((time.perf_counter() - started) * 1000),
                detail=f"attempts={attempt}; {result.detail}",
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
        except Exception as error:  # adapter failures must be logged and retried
            last_error = str(error)
            session.add(db.ActionExecutionRecord(
                request_id=record.id,
                checklist_item_id=item.id,
                action=action,
                actor_subject=actor.subject,
                adapter=ACTION_ADAPTER.__class__.__name__,
                correlation_id=correlation_id,
                attempt=attempt,
                status="retrying" if attempt < attempts_limit else "failed",
                error_category="adapter_error",
                error_detail=last_error[:1000],
                started_at=started_at,
                completed_at=db.utcnow(),
                duration_ms=int((time.perf_counter() - started) * 1000),
            ))

    _notify_action_failure(action, context, attempts_limit, last_error, session)
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action=f"onboarding.action.{action}",
        status="failure",
        duration_ms=int((time.perf_counter() - started) * 1000),
        detail=f"attempts={attempts_limit}; {last_error}",
    )
    session.flush()
    session.commit()
    raise HTTPException(
        502,
        f"Action '{action}' failed after {attempts_limit} attempts.",
    )


@app.post("/onboarding-requests/{request_id}/items/{item_id}/execute")
def execute_item(
    request_id: int,
    item_id: int,
    authorization: Optional[str] = Header(default=None),
    session: Session = Depends(db.get_session),
) -> OnboardingRequestOut:
    """Execute a checklist action through the configured Microsoft adapter."""
    try:
        actor = authenticate_bearer(authorization)
    except AuthenticationError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    return _execute_item(session, request_id, item_id, actor)

@app.get("/onboarding-requests/{request_id}/action-executions", response_model=list[ActionExecutionOut])
def list_action_executions(
    request_id: int, session: Session = Depends(db.get_session)
) -> list[ActionExecutionOut]:
    """List durable action attempts for one onboarding request."""
    _load_request(session, request_id)
    records = session.execute(
        select(db.ActionExecutionRecord)
        .where(db.ActionExecutionRecord.request_id == request_id)
        .order_by(db.ActionExecutionRecord.id)
    ).scalars().all()
    return [ActionExecutionOut.model_validate(record, from_attributes=True) for record in records]


@app.post("/onboarding-requests/{request_id}/items/{item_id}/approve")
def approve_item(
    request_id: int, item_id: int, session: Session = Depends(db.get_session)
) -> OnboardingRequestOut:
    """Approve a checklist step that requires human sign-off."""
    return _transition_item(session, request_id, item_id, "approve")


@app.post("/onboarding-requests/{request_id}/items/{item_id}/complete")
def complete_item(
    request_id: int, item_id: int, session: Session = Depends(db.get_session)
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
    if record.request_type == "offboarding":
        raise HTTPException(
            409,
            "Offboarding steps must be completed via "
            "POST /offboarding-requests/{checklist_id}/steps/{step_key}/complete "
            "(idempotent completion ledger).",
        )
    return _transition_item(session, request_id, item_id, "complete")


class OffboardingStepResultOut(BaseModel):
    """Stored outcome of one offboarding step (v0.6.1)."""

    step_key: StrictStr
    status: str
    executor_called: bool
    executed_at: datetime


class OffboardingCompletionOut(BaseModel):
    """Response body of the idempotent offboarding completion endpoint."""

    checklist_id: int
    employee_name: str
    step: OffboardingStepResultOut


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


@app.post(
    "/offboarding-requests/{checklist_id}/steps/{step_key}/complete",
    response_model=OffboardingCompletionOut,
)
def complete_offboarding_step(
    checklist_id: int,
    step_key: str,
    response: Response,
    session: Session = Depends(db.get_session),
) -> OffboardingCompletionOut:
    """Idempotently complete one offboarding step (v0.6.1 hardening).

    Side effects (account disable, mailbox archive, ...) must never run
    twice for the same step, even when callers retry with different
    request bodies or headers. The ``offboarding_completions`` table is
    the ledger: a stored ``(checklist_id, step_key)`` row short-circuits
    re-execution and its ``result_json`` is replayed with HTTP 200 and
    the header ``X-Offboarding-Replayed: true``. A first execution runs
    the module-level ``offboarding_executor`` (simulated), stores the
    result, and returns HTTP 201.

    Design note — why onboarding differs: onboarding steps keep their own
    per-item state machine (pending/approved/completed plus a ``result``
    column) because provisioning is additive and safe to re-inspect.
    Offboarding destroys access, so it needs a separate append-only ledger
    keyed by ``(checklist_id, step_key)`` that survives checklist edits.
    Unifying both into one mechanism is deliberately left out of this
    small change; see README limitations.

    Validation: ``step_key`` must belong to this checklist (unknown keys
    give 422); the checklist must be an offboarding request (otherwise
    409); closed or abandoned checklists reject completion with 409.
    Every path — first run, replay, rejection — writes an execution log.
    """
    started = time.perf_counter()
    # Load directly (not via _load_request) so a missing id gives 404 while
    # an onboarding id can produce a more helpful 409 below.
    record = session.get(db.OnboardingRequestRecord, checklist_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Offboarding checklist not found.")
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
        )

    if record.request_type != "offboarding":
        log("rejected", "failure", "request is not an offboarding checklist")
        session.commit()  # persist the rejection log before raising
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

    existing = session.execute(
        select(db.OffboardingCompletionRecord).where(
            db.OffboardingCompletionRecord.checklist_id == checklist_id,
            db.OffboardingCompletionRecord.step_key == step_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        stored = json.loads(existing.result_json)
        log("replayed", "success", f"returning stored result from ledger #{existing.id}")
        response.headers["X-Offboarding-Replayed"] = "true"
        response.status_code = http_status.HTTP_200_OK
        return OffboardingCompletionOut.model_validate(stored)

    item = next(i for i in items if i.step_key == step_key)
    if item.requires_approval and item.status != "approved":
        log("rejected", "failure", f"step requires approval (status '{item.status}')")
        session.commit()
        raise HTTPException(
            409,
            f"Step '{step_key}' requires approval before completion "
            f"(current status: '{item.status}'). Approve it via "
            "POST /onboarding-requests/{request_id}/items/{item_id}/approve.",
        )

    # First execution: run the (simulated) side effect, then claim the
    # ledger row. If another concurrent request already claimed it, the
    # unique constraint raises IntegrityError — we roll back (discarding
    # this attempt's partial work) and replay the winner's stored result
    # instead of crashing or double-executing anything durable.
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
        {
            **body.model_dump(mode="json"),
            "simulated_result": result_payload,
        },
        separators=(",", ":"),
    )
    session.add(
        db.OffboardingCompletionRecord(
            checklist_id=checklist_id,
            step_key=step_key,
            result_json=stored_json,
        )
    )
    item.status = "completed"
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        competing = session.execute(
            select(db.OffboardingCompletionRecord).where(
                db.OffboardingCompletionRecord.checklist_id == checklist_id,
                db.OffboardingCompletionRecord.step_key == step_key,
            )
        ).scalar_one()
        log(
            "replayed",
            "success",
            f"lost a race for the ledger row; replayed result #{competing.id}",
        )
        response.headers["X-Offboarding-Replayed"] = "true"
        response.status_code = http_status.HTTP_200_OK
        return OffboardingCompletionOut.model_validate(
            json.loads(competing.result_json)
        )

    log("executed", "success", f"simulated executor result: {result_payload['action']}")
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
        )
    response.status_code = http_status.HTTP_201_CREATED
    return body


class M365StatusOut(BaseModel):
    """Shape of the *simulated* M365 status response (v0.6 placeholder)."""

    simulated: StrictBool
    id: StrictStr
    provisioning_state: StrictStr
    last_sync: StrictStr


@app.get("/onboarding-requests/m365-status/{user_id}", response_model=M365StatusOut)
def get_m365_status(user_id: str) -> M365StatusOut:
    """Look up a *simulated* M365 provisioning status by user id.

    This never contacts real Microsoft services; see src/m365_simulator.py.
    """
    return M365StatusOut.model_validate(m365_simulator.get_status(user_id))


# ---------------------------------------------------------------------------
# Routes: monitoring


class NotificationOutboxOut(BaseModel):
    """One stored notification delivery, exposed for operators (v1.0 preview)."""

    id: int
    url: str
    ticket_id: Optional[str] = None
    status: str
    attempts: int
    last_error: Optional[str] = None
    created_at: datetime


def _outbox_out(record: db.NotificationOutboxRecord) -> NotificationOutboxOut:
    return NotificationOutboxOut(
        **{c: getattr(record, c) for c in (
            "id", "url", "ticket_id", "status", "attempts",
            "last_error", "created_at",
        )}
    )


@app.get("/notifications/pending", response_model=list[NotificationOutboxOut])
def list_pending_notifications(
    session: Session = Depends(db.get_session),
) -> list[NotificationOutboxOut]:
    """List webhook deliveries that await a manual retry."""
    records = session.execute(
        select(db.NotificationOutboxRecord)
        .where(db.NotificationOutboxRecord.status == "pending")
        .order_by(db.NotificationOutboxRecord.id)
    ).scalars().all()
    return [_outbox_out(record) for record in records]


@app.get("/notifications/failed", response_model=list[NotificationOutboxOut])
def list_failed_notifications(
    session: Session = Depends(db.get_session),
) -> list[NotificationOutboxOut]:
    """List dead-letter deliveries that exhausted their retry budget.

    Operators can inspect these rows and return one to the pending queue with
    ``POST /notifications/{notification_id}/requeue``.
    """
    records = session.execute(
        select(db.NotificationOutboxRecord)
        .where(db.NotificationOutboxRecord.status == "failed")
        .order_by(db.NotificationOutboxRecord.id)
    ).scalars().all()
    return [_outbox_out(record) for record in records]


@app.post("/notifications/retry", response_model=list[NotificationOutboxOut])
def retry_failed_notifications(
    session: Session = Depends(db.get_session),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[NotificationOutboxOut]:
    """Retry pending webhook deliveries once each (oldest first).

    Successful retries are marked ``delivered``; still-failing ones stay
    ``pending`` until their attempt count reaches
    ``FLOWOPS_NOTIFICATION_MAX_ATTEMPTS``, at which point they become
    ``failed`` (dead-lettered). Every outcome is written to the execution log.
    """
    max_attempts = _notification_max_attempts()
    records = session.execute(
        select(db.NotificationOutboxRecord)
        .where(db.NotificationOutboxRecord.status == "pending")
        .order_by(db.NotificationOutboxRecord.id)
        .limit(limit)
    ).scalars().all()
    results = []
    for record in records:
        payload = json.loads(record.payload_json)
        started = time.perf_counter()
        # One bounded retry per call; the operator can call again later.
        delivery = deliver_webhook(record.url, payload, max_attempts=1)
        duration_ms = int((time.perf_counter() - started) * 1000)
        record.attempts += delivery.attempts
        if delivery.success:
            record.status = "delivered"
            record.last_error = None
        else:
            record.last_error = delivery.last_error
            if record.attempts >= max_attempts:
                record.status = "failed"
                db.log_execution(
                    session,
                    run_id=_new_run_id(),
                    action="notification.dead_lettered",
                    status="failure",
                    ticket_id=record.ticket_id,
                    duration_ms=duration_ms,
                    detail=(
                        f"outbox #{record.id} exhausted after "
                        f"{record.attempts} attempts"
                    ),
                )
        db.log_execution(
            session,
            run_id=_new_run_id(),
            action="notification.retry",
            status="success" if delivery.success else "failure",
            ticket_id=record.ticket_id,
            duration_ms=duration_ms,
            detail=f"outbox #{record.id}: now '{record.status}'",
        )
        results.append(_outbox_out(record))
    return results


@app.post(
    "/notifications/{notification_id}/requeue",
    response_model=NotificationOutboxOut,
)
def requeue_failed_notification(
    notification_id: int, session: Session = Depends(db.get_session)
) -> NotificationOutboxOut:
    """Return one dead-letter delivery to the pending queue.

    The transition is conditional on the row still being ``failed``, so two
    operators cannot requeue the same delivery twice. Attempt history and the
    last error are preserved; each requeue grants one more delivery attempt.
    """
    record = session.get(db.NotificationOutboxRecord, notification_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Notification delivery not found.")
    if record.status != "failed":
        raise HTTPException(
            status_code=409,
            detail=f"Only failed deliveries can be requeued (current status: '{record.status}').",
        )

    table = db.NotificationOutboxRecord.__table__
    now = db.utcnow()
    result = session.execute(
        update(table)
        .where(
            and_(
                table.c.id == notification_id,
                table.c.status == "failed",
            )
        )
        .values(status="pending", updated_at=now)
    )
    if result.rowcount != 1:
        raise HTTPException(
            status_code=409,
            detail="Notification delivery changed state; refresh and retry.",
        )
    session.expire_all()
    record = session.get(db.NotificationOutboxRecord, notification_id)
    if record is None:  # pragma: no cover - only if deleted concurrently
        raise HTTPException(status_code=404, detail="Notification delivery not found.")
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action="notification.requeued",
        status="success",
        ticket_id=record.ticket_id,
        duration_ms=0,
        detail=(
            f"outbox #{record.id} returned to pending; "
            f"attempts={record.attempts} preserved"
        ),
    )
    return _outbox_out(record)


@app.get("/execution-logs", response_model=list[ExecutionLogOut])
def list_execution_logs(
    session: Session = Depends(db.get_session),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ExecutionLogOut]:
    """Recent workflow execution log entries, newest first."""
    statement = select(db.ExecutionLogRecord).order_by(
        db.ExecutionLogRecord.id.desc()
    ).limit(limit)
    if status_filter in ("success", "failure"):
        statement = statement.where(db.ExecutionLogRecord.status == status_filter)
    records = session.execute(statement).scalars().all()
    return [
        ExecutionLogOut(
            **{c: getattr(record, c) for c in (
                "id", "run_id", "action", "status", "ticket_id",
                "duration_ms", "detail", "created_at",
            )}
        )
        for record in records
    ]


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard(
    request: Request,
    session: Session = Depends(db.get_session),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    auto_refresh: bool = Query(default=False),
) -> HTMLResponse:
    """Server-rendered monitoring dashboard over tickets and execution logs."""
    tickets = session.execute(select(db.TicketRecord)).scalars().all()
    log_query = select(db.ExecutionLogRecord).order_by(
        db.ExecutionLogRecord.id.desc()
    ).limit(200)
    if status_filter in ("success", "failure"):
        log_query = log_query.where(db.ExecutionLogRecord.status == status_filter)
    logs = session.execute(log_query).scalars().all()
    outbox_records = session.execute(
        select(db.NotificationOutboxRecord)
    ).scalars().all()
    pending_deliveries = [
        item for item in outbox_records if item.status == "pending"
    ]
    failed_deliveries = [
        item for item in outbox_records if item.status == "failed"
    ]

    total = len(tickets)
    open_count = sum(1 for t in tickets if t.status == "open")
    closed_count = total - open_count
    auto_routed = sum(
        1 for t in tickets if t.queue and t.queue != "it-service-desk"
    )
    success_count = sum(1 for log in logs if log.status == "success")
    failure_count = len(logs) - success_count
    avg_duration = (
        round(sum(log.duration_ms for log in logs) / len(logs)) if logs else 0
    )
    outbox_counts = Counter(item.status for item in outbox_records)

    def bars(counter: Counter) -> list[dict[str, object]]:
        maximum = max(counter.values(), default=0)
        return [
            {
                "label": str(label),
                "count": count,
                "percent": round((count / maximum) * 100) if maximum else 0,
            }
            for label, count in counter.most_common()
        ]

    stats = {
        "total": total,
        "open": open_count,
        "closed": closed_count,
        "auto_routed": auto_routed,
        "success_count": success_count,
        "failure_count": failure_count,
        "avg_duration": avg_duration,
        "hours_saved": round(auto_routed * 15 / 60, 1),
        "pending_notifications": outbox_counts.get("pending", 0),
        "delivered_notifications": outbox_counts.get("delivered", 0),
        "failed_notifications": outbox_counts.get("failed", 0),
        "notification_retries": sum(
            max(item.attempts - 1, 0) for item in outbox_records
        ),
    }
    log_rows = [
        {
            "id": log.id,
            "created_at": log.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "action": log.action,
            "status": log.status,
            "ticket_id": log.ticket_id,
            "duration_ms": log.duration_ms,
        }
        for log in logs[:50]
    ]
    return web.render(
        request,
        "dashboard.html",
        {
            "active": "dashboard",
            "stats": stats,
            "queues": bars(Counter(t.queue or "unrouted" for t in tickets)),
            "priorities": bars(Counter(t.priority for t in tickets)),
            "logs": log_rows,
            "status_filter": status_filter,
            "pending": pending_deliveries,
            "failed": failed_deliveries,
            "auto_refresh": auto_refresh,
            "last_updated": db.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        },
    )

    outbox_card = (
        f'<div class="card"><div class="value fail">{outbox_pending}'
        '</div><div class="label">pending webhook retries</div></div>'
        if outbox_pending
        else ""
    )
    outbox_section = ""
    if pending_deliveries:
        outbox_rows = "".join(
            "<tr>"
            f"<td>#{item.id}</td>"
            f"<td>{html.escape(item.ticket_id or '')}</td>"
            f"<td>{html.escape(item.url)}</td>"
            f"<td>{item.attempts}</td>"
            f"<td class=\"fail\">{html.escape(item.last_error or '')}</td>"
            "</tr>"
            for item in pending_deliveries[:15]
        )
        outbox_section = f"""<section>
        <h2>Failed webhook deliveries ({outbox_pending} pending)</h2>
<p>Retry them with <code>POST /notifications/retry</code> &middot;
list with <code>GET /notifications/pending</code>.</p>
<table><tr><th>#</th><th>ticket</th><th>url</th><th>attempts</th><th>last error</th></tr>{outbox_rows}</table>
</section>
"""

    # v0.6 placeholder: simulated M365 user ids recorded by completed
    # provisioning steps, clearly labelled so nobody mistakes them for real.
    m365_lines: list[str] = []
    # v0.6.1: offboarding progress per checklist (completed vs total steps),
    # read from the same rows the ledger endpoint uses.
    offboarding_progress_rows: list[str] = []
    for onb_record in session.execute(
        select(db.OnboardingRequestRecord)
    ).scalars().all():
        onb_items = session.execute(
            select(db.ChecklistItemRecord).where(
                db.ChecklistItemRecord.request_id == onb_record.id
            )
        ).scalars().all()
        for onb_item in sorted(onb_items, key=lambda i: i.id):
            if onb_item.action == "m365_provision" and onb_item.result:
                try:
                    sim_user_id = json.loads(onb_item.result).get("id", "?")
                except (ValueError, TypeError):
                    sim_user_id = "?"
                m365_lines.append(
                    "<li>"
                    f"request #{onb_record.id} &middot; {html.escape(onb_record.employee_name)} "
                    f"&middot; simulated M365 id <code>{html.escape(str(sim_user_id))}</code> "
                    "(simulated)"
                    "</li>"
                )
        if onb_record.request_type == "offboarding":
            done = sum(1 for i in onb_items if i.status == "completed")
            offboarding_progress_rows.append(
                "<tr>"
                f"<td>#{onb_record.id}</td>"
                f"<td>{html.escape(onb_record.employee_name)}</td>"
                f"<td>{html.escape(onb_record.status)}</td>"
                f"<td>{done} / {len(onb_items)}</td>"
                "</tr>"
            )
    m365_section = (
        "<section><h2>Simulated Microsoft 365 provisioning</h2>"
        "<p>v0.6 placeholder &mdash; these accounts are fake; no real "
        "Microsoft 365 tenant is touched.</p>"
        f"<ul>{''.join(m365_lines)}</ul></section>\n"
        if m365_lines
        else ""
    )
    offboarding_section = (
        "<section><h2>Offboarding progress</h2>"
        "<p>Completed steps per offboarding checklist (v0.6.1 ledger).</p>"
        "<table><tr><th>#</th><th>employee</th><th>request status</th>"
        f"<th>steps</th></tr>{''.join(offboarding_progress_rows)}</table>"
        "</section>\n"
        if offboarding_progress_rows
        else ""
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FlowOps dashboard</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 2rem; background: #f5f6f8; color: #1f2933; }}
  h1 {{ font-size: 1.5rem; }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 1rem; margin: 1rem 0 2rem; }}
  .card {{ background: #fff; border-radius: 10px; padding: 1rem 1.5rem; box-shadow: 0 1px 4px rgba(0,0,0,.08); min-width: 9rem; }}
  .card .value {{ font-size: 1.8rem; font-weight: 600; }}
  .card .label {{ color: #5f6b7a; font-size: .8rem; }}
  section {{ background: #fff; border-radius: 10px; padding: 1rem 1.5rem; box-shadow: 0 1px 4px rgba(0,0,0,.08); margin-bottom: 1.5rem; overflow-x: auto; }}
  table {{ border-collapse: collapse; width: 100%; font-size: .85rem; }}
  th, td {{ text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #eef0f3; }}
  th {{ color: #5f6b7a; font-weight: 600; }}
  td.ok {{ color: #137333; }} td.fail {{ color: #c5221f; font-weight: 600; }}
  .card .value.fail {{ color: #c5221f; }}
  .cols {{ display: flex; gap: 1.5rem; flex-wrap: wrap; }}
  .cols section {{ flex: 1; min-width: 16rem; }}
  a {{ color: #2563eb; }}
</style>
</head>
<body>
<h1>FlowOps monitoring dashboard</h1>
<p><a href="/">&larr; back</a> &middot; learning prototype, simulated data</p>
<div class="cards">
  <div class="card"><div class="value">{total}</div><div class="label">tickets</div></div>
  <div class="card"><div class="value">{open_count}</div><div class="label">open</div></div>
  <div class="card"><div class="value">{closed_count}</div><div class="label">closed</div></div>
  <div class="card"><div class="value">{auto_routed}</div><div class="label">auto-routed</div></div>
  <div class="card"><div class="value">{success_count}</div><div class="label">successful runs</div></div>
  <div class="card"><div class="value">{failure_count}</div><div class="label">failed runs</div></div>
  <div class="card"><div class="value">{avg_duration} ms</div><div class="label">avg run duration</div></div>
  <div class="card"><div class="value">{hours_saved} h</div><div class="label">est. staff time saved</div></div>
  <div class="card"><div class="value">{outbox_pending}</div><div class="label">pending notifications</div></div>
  <div class="card"><div class="value">{outbox_pending}</div><div class="label">pending webhook retries</div></div>
  <div class="card"><div class="value">{outbox_delivered}</div><div class="label">delivered notifications</div></div>
  <div class="card"><div class="value">{outbox_failed}</div><div class="label">failed notifications</div></div>
  <div class="card"><div class="value">{outbox_retries}</div><div class="label">notification retries</div></div>
</div>
<div class="cols">
  <section><h2>Tickets by queue</h2><table>{counter_rows(by_queue)}</table></section>
  <section><h2>Tickets by priority</h2><table>{counter_rows(by_priority)}</table></section>
</div>
{outbox_section}{m365_section}{offboarding_section}<section>
  <h2>Recent workflow runs</h2>
  <table><tr><th>run</th><th>time</th><th>action</th><th>status</th><th>ticket</th><th>duration</th></tr>{recent_rows}</table>
</section>
</body>
</html>"""
