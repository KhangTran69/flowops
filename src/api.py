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
import secrets
import threading
import time
import uuid
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, Optional
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query, Request, Response
from fastapi import status as http_status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator
from sqlalchemy import and_, delete, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from src import ai_agent, db, jobs, ui_session, web  # noqa: F401 - modules referenced at runtime
from src.actions import (
    ACTION_BY_STEP,
    ActionAdapter,
    ActionResult,
    SimulatedMicrosoftAdapter,
    action_for_step,
    adapter_from_environment,
)
from src.auth import (
    AuthenticationError,
    Actor,
    authenticate_bearer,
    validate_auth_configuration,
)
from src.m365_simulator import M365Simulator
from src.notify import deliver_webhook
from src.onboarding import checklist_for
from src.routing import route_ticket
from src.ticket_processor import normalize_ticket

ACTION_ADAPTER: ActionAdapter = adapter_from_environment()
AI_ADAPTER: ai_agent.AIAdapter = ai_agent.adapter_from_environment()
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


def _local_ui_actor() -> Actor:
    return Actor(
        subject="local-development",
        roles=("automation.operator", "automation.admin"),
        source="local",
    )


def _actor_from_header(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> Actor:
    """Resolve a bearer actor or an authenticated browser session."""
    if authorization:
        try:
            actor = authenticate_bearer(authorization)
        except AuthenticationError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        return replace(actor, source="bearer")
    session = ui_session.read_session(request)
    if session is not None:
        return ui_session.actor_from_session(session)
    try:
        actor = authenticate_bearer(None)
    except AuthenticationError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    if ui_session.ui_auth_required() and actor.source == "local":
        raise HTTPException(status_code=401, detail="Authentication required.")
    return actor


def _enforce_csrf(request: Optional[Request], actor: Actor) -> None:
    """CSRF applies only to cookie-authenticated browser mutations."""
    if request is None or actor.source != "session":
        return
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    session = ui_session.read_session(request)
    if session is None or not ui_session.csrf_matches(request, session):
        raise HTTPException(
            status_code=403,
            detail="CSRF token missing or invalid; refresh and retry.",
        )


def _operator_actor(
    request: Request,
    actor: Actor = Depends(_actor_from_header),
) -> Actor:
    if not {"automation.operator", "automation.admin"} & set(actor.roles):
        raise HTTPException(status_code=403, detail="Operator role is required.")
    _enforce_csrf(request, actor)
    _enforce_rate_limit(f"actor:{actor.subject}")
    return actor


def _admin_actor(
    request: Request,
    actor: Actor = Depends(_actor_from_header),
) -> Actor:
    if "automation.admin" not in actor.roles:
        raise HTTPException(status_code=403, detail="Admin role is required.")
    _enforce_csrf(request, actor)
    _enforce_rate_limit(f"actor:{actor.subject}")
    return actor


def _webhook_actor(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> str:
    expected = os.environ.get("FLOWOPS_WEBHOOK_API_KEY")
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid webhook API key.")
    _enforce_rate_limit("webhook:intake")
    return "webhook-api-key" if expected else "local-development"


_METRICS_LOCK = threading.Lock()
_METRICS: dict[tuple[str, str, int], int] = {}
_METRICS_STARTED = time.time()
_AI_METRICS_LOCK = threading.Lock()
_AI_TRIAGE: dict[tuple[str, str], int] = {}
_AI_APPLY: dict[str, int] = {}
_AI_PROVIDER_ERRORS: dict[str, int] = {}


def _ai_provider_name() -> str:
    return str(getattr(AI_ADAPTER, "provider", getattr(AI_ADAPTER, "name", "unknown")))


def _record_ai_triage(adapter: str, outcome: str) -> None:
    with _AI_METRICS_LOCK:
        key = (adapter, outcome)
        _AI_TRIAGE[key] = _AI_TRIAGE.get(key, 0) + 1


def _record_ai_apply(field: str) -> None:
    with _AI_METRICS_LOCK:
        _AI_APPLY[field] = _AI_APPLY.get(field, 0) + 1


def _record_ai_provider_error(provider: str) -> None:
    with _AI_METRICS_LOCK:
        _AI_PROVIDER_ERRORS[provider] = _AI_PROVIDER_ERRORS.get(provider, 0) + 1


def _record_request(method: str, route: str, status_code: int, duration: float) -> None:
    key = (method, route, status_code)
    with _METRICS_LOCK:
        _METRICS[key] = _METRICS.get(key, 0) + 1


@app.middleware("http")
async def correlation_and_metrics(request: Request, call_next):
    """Attach a correlation id, resolve UI sessions, and count requests."""
    correlation_id = request.headers.get("X-Correlation-ID") or uuid.uuid4().hex
    request.state.correlation_id = correlation_id
    session = ui_session.read_session(request)
    request.state.ui_session = session
    request.state.actor = (
        ui_session.actor_from_session(session) if session else _local_ui_actor()
    )
    request.state.csrf_token = session.csrf_token if session else ""
    protected_ui_path = request.url.path == "/dashboard" or request.url.path.startswith(
        "/ui/"
    )
    if ui_session.ui_auth_required() and protected_ui_path and session is None:
        redirect = RedirectResponse(
            f"/login?next={quote(request.url.path)}", status_code=303
        )
        redirect.headers["X-Correlation-ID"] = correlation_id
        return redirect
    started = time.perf_counter()
    response = await call_next(request)
    duration = time.perf_counter() - started
    route = request.scope.get("route")
    route_path = getattr(route, "path", request.url.path)
    _record_request(request.method, route_path, response.status_code, duration)
    response.headers["X-Correlation-ID"] = correlation_id
    return response


_RATE_LIMIT_LOCK = threading.Lock()
_RATE_LIMIT_BUCKETS: dict[str, tuple[float, int]] = {}


def _enforce_rate_limit(actor_key: str) -> None:
    """Simple in-process token bucket for the prototype (disabled by default)."""
    try:
        limit = int(os.environ.get("FLOWOPS_RATE_LIMIT_PER_MINUTE", "0") or 0)
    except ValueError:
        limit = 0
    if limit <= 0:
        return
    now = time.monotonic()
    with _RATE_LIMIT_LOCK:
        window_start, count = _RATE_LIMIT_BUCKETS.get(actor_key, (now, 0))
        if now - window_start >= 60:
            window_start, count = now, 0
        if count >= limit:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded; retry later.",
                headers={"Retry-After": "60"},
            )
        _RATE_LIMIT_BUCKETS[actor_key] = (window_start, count + 1)


@app.on_event("startup")
def _validate_auth_on_startup() -> None:
    validate_auth_configuration()
    ui_session.validate_ui_auth_configuration()


@app.get("/auth/me")
def auth_me(actor: Actor = Depends(_actor_from_header)) -> dict:
    """Return the resolved local, bearer, or session actor."""
    return {
        "subject": actor.subject,
        "roles": list(actor.roles),
        "source": actor.source,
    }


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


# Recovery lease for in-flight idempotency reservations (README: abandoned
# requests can be reclaimed after five minutes). Used by the claim/takeover
# logic below and by _reservation_is_stale().
IDEMPOTENCY_PROCESSING_TIMEOUT = timedelta(minutes=5)


def _request_fingerprint(request: BaseModel) -> str:
    """Return a stable SHA-256 of a validated request body."""
    return hashlib.sha256(
        json.dumps(request.model_dump(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _validate_idempotency_key(key: Optional[str]) -> None:
    if key is not None and len(key) > 128:
        raise HTTPException(
            status_code=422, detail="Idempotency-Key must be at most 128 characters."
        )


def _stored_resource_type(payload: dict[str, Any]) -> str:
    """Legacy records predate resource scoping and are ticket reservations."""
    return payload.get("resource_type", IDEMPOTENCY_SCOPE_TICKET)


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


class KnowledgeBaseArticleOut(BaseModel):
    id: str
    title: str
    category: str
    keywords: list[str]
    steps: list[str]
    summary: str


class KnowledgeBaseMatchOut(BaseModel):
    article: KnowledgeBaseArticleOut
    score: int


class AISummaryOut(BaseModel):
    text: str
    requester_id: int
    title: str
    status: str
    priority: str
    category: Optional[str] = None
    queue: Optional[str] = None
    urgency_signals: list[str]
    history_count: int


class AIRecommendationOut(BaseModel):
    id: str
    kind: str
    title: str
    current: Optional[str] = None
    suggested: Optional[str] = None
    rationale: str
    confidence: int
    applicable: bool


class AITriageOut(BaseModel):
    ticket_id: str
    summary: AISummaryOut
    knowledge_base: list[KnowledgeBaseMatchOut]
    recommendations: list[AIRecommendationOut]
    adapter: str
    generated_at: datetime


class AIApplyRequest(BaseModel):
    recommendation_ids: list[StrictStr] = Field(min_length=1)


class AIApplyOut(BaseModel):
    ticket: Ticket
    applied: list[str]


class WorkflowJobOut(BaseModel):
    """Durable workflow queue entry exposed to operators."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    job_type: str
    status: str
    attempts: int
    max_attempts: int
    available_at: datetime
    last_error: Optional[str] = None
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    label: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None


class ExecutionLogOut(BaseModel):
    id: int
    run_id: str
    action: str
    status: str
    ticket_id: Optional[str] = None
    duration_ms: int
    detail: Optional[str] = None
    actor_subject: Optional[str] = None
    correlation_id: Optional[str] = None
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


def _process_intake(
    request: TicketIntakeRequest,
    session: Session,
    actor_subject: Optional[str] = None,
) -> Ticket:
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
        actor_subject=actor_subject,
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


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(
    request: Request,
    next: str = Query(default=""),
    error: str = Query(default=""),
) -> Response:
    """Render the local token login page."""
    if not ui_session.ui_auth_required():
        return RedirectResponse("/dashboard", status_code=303)
    if ui_session.read_session(request) is not None:
        return RedirectResponse(ui_session.safe_next(next), status_code=303)
    return web.render(
        request,
        "login.html",
        {
            "active": "login",
            "next": ui_session.safe_next(next),
            "error": error,
        },
    )


@app.post("/login", response_class=HTMLResponse, include_in_schema=False)
def login_submit(
    request: Request,
    token: str = Form(default=""),
    next: str = Form(default=""),
    db_session: Session = Depends(db.get_session),
) -> Response:
    """Validate a bearer token and create a signed browser session."""
    if not ui_session.ui_auth_required():
        return RedirectResponse("/dashboard", status_code=303)
    try:
        actor = authenticate_bearer(f"Bearer {token.strip()}")
    except AuthenticationError:
        return web.render(
            request,
            "login.html",
            {
                "active": "login",
                "next": ui_session.safe_next(next),
                "error": "Invalid access token.",
            },
            status_code=401,
        )
    ui_data = ui_session.create_session(actor)
    response = RedirectResponse(ui_session.safe_next(next), status_code=303)
    ui_session.set_session_cookie(response, ui_data)
    db.log_execution(
        db_session,
        run_id=_new_run_id(),
        action="ui.login",
        status="success",
        duration_ms=0,
        detail=f"source=session; roles={','.join(actor.roles)}",
        actor_subject=actor.subject,
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    db_session.commit()
    return response


@app.post("/logout", response_class=HTMLResponse, include_in_schema=False)
def logout(
    request: Request,
    csrf_token: str = Form(default=""),
    session_db: Session = Depends(db.get_session),
) -> Response:
    """Clear the browser session after a CSRF check."""
    session = ui_session.read_session(request)
    if session is None:
        response = RedirectResponse("/login", status_code=303)
        ui_session.clear_session_cookie(response)
        return response
    if not csrf_token or not secrets.compare_digest(csrf_token, session.csrf_token):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid.")
    db.log_execution(
        session_db,
        run_id=_new_run_id(),
        action="ui.logout",
        status="success",
        duration_ms=0,
        detail="source=session",
        actor_subject=session.subject,
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    session_db.commit()
    response = RedirectResponse("/login", status_code=303)
    ui_session.clear_session_cookie(response)
    return response


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


@app.get("/ready")
def ready() -> dict:
    """Readiness probe: verifies the configured database is reachable."""
    try:
        with db.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as error:  # pragma: no cover - exercised by deployment
        raise HTTPException(status_code=503, detail="Database is not ready.") from error
    return {"status": "ready", "database": "ok"}


@app.get("/metrics", include_in_schema=False)
def metrics(session: Session = Depends(db.get_session)) -> Response:
    """Minimal Prometheus-style metrics for jobs, outbox, and HTTP requests."""
    lines = [
        "# HELP flowops_http_requests_total HTTP requests by method, route, and status",
        "# TYPE flowops_http_requests_total counter",
    ]
    with _METRICS_LOCK:
        counters = dict(_METRICS)
    for (method, route, status_code), count in sorted(counters.items()):
        lines.append(
            "flowops_http_requests_total"
            f'{{method="{method}",route="{route}",status="{status_code}"}} {count}'
        )

    job_records = session.execute(select(db.WorkflowJobRecord)).scalars().all()
    job_counts = Counter(job.status for job in job_records)
    lines.extend(
        [
            "# HELP flowops_workflow_jobs Workflow jobs by state",
            "# TYPE flowops_workflow_jobs gauge",
        ]
    )
    for state in jobs.JOB_STATUSES:
        lines.append(f'flowops_workflow_jobs{{status="{state}"}} {job_counts.get(state, 0)}')

    outbox = session.execute(select(db.NotificationOutboxRecord)).scalars().all()
    outbox_counts = Counter(item.status for item in outbox)
    lines.extend(
        [
            "# HELP flowops_notification_outbox Notification deliveries by state",
            "# TYPE flowops_notification_outbox gauge",
        ]
    )
    for state in ("pending", "delivered", "failed"):
        lines.append(
            f'flowops_notification_outbox{{status="{state}"}} {outbox_counts.get(state, 0)}'
        )
    with _AI_METRICS_LOCK:
        triage_counts = dict(_AI_TRIAGE)
        apply_counts = dict(_AI_APPLY)
        provider_errors = dict(_AI_PROVIDER_ERRORS)
    lines.extend(
        [
            "# HELP flowops_ai_triage_total AI triage calls by adapter and outcome",
            "# TYPE flowops_ai_triage_total counter",
        ]
    )
    for (adapter, outcome), count in sorted(triage_counts.items()):
        lines.append(
            f'flowops_ai_triage_total{{adapter="{adapter}",outcome="{outcome}"}} {count}'
        )
    lines.extend(
        [
            "# HELP flowops_ai_apply_total Applied AI recommendation fields",
            "# TYPE flowops_ai_apply_total counter",
        ]
    )
    for field in sorted(apply_counts):
        lines.append(f'flowops_ai_apply_total{{field="{field}"}} {apply_counts[field]}')
    lines.extend(
        [
            "# HELP flowops_ai_provider_errors_total AI provider failures",
            "# TYPE flowops_ai_provider_errors_total counter",
        ]
    )
    for provider in sorted(provider_errors):
        lines.append(
            f'flowops_ai_provider_errors_total{{provider="{provider}"}} {provider_errors[provider]}'
        )
    lines.append(f"flowops_uptime_seconds {time.time() - _METRICS_STARTED:.1f}")
    return Response("\n".join(lines) + "\n", media_type="text/plain")


# ---------------------------------------------------------------------------
# Routes: tickets


@app.post("/tickets", response_model=Ticket, status_code=http_status.HTTP_201_CREATED)
def create_ticket(
    request: TicketIntakeRequest,
    response: Response,
    actor: Actor = Depends(_operator_actor),
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
        result = _process_intake(request, session, actor.subject)
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


# ---------------------------------------------------------------------------
# Routes: AI triage and knowledge base


def _load_ticket(session: Session, ticket_id: str) -> db.TicketRecord:
    if not ticket_id.isdigit():
        raise HTTPException(status_code=404, detail="Ticket not found.")
    record = session.get(db.TicketRecord, ticket_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    return record


def _ticket_history(
    session: Session, ticket_id: str, limit: int = 20
) -> list[dict[str, Any]]:
    records = session.execute(
        select(db.ExecutionLogRecord)
        .where(db.ExecutionLogRecord.ticket_id == ticket_id)
        .order_by(db.ExecutionLogRecord.id.desc())
        .limit(limit)
    ).scalars().all()
    return [
        {
            "action": row.action,
            "status": row.status,
            "detail": row.detail,
            "created_at": row.created_at,
        }
        for row in records
    ]


def _article_out(article: ai_agent.KnowledgeBaseArticle) -> KnowledgeBaseArticleOut:
    return KnowledgeBaseArticleOut(
        id=article.id,
        title=article.title,
        category=article.category,
        keywords=list(article.keywords),
        steps=list(article.steps),
        summary=article.summary,
    )


def _triage_out(result: ai_agent.TriageResult) -> AITriageOut:
    return AITriageOut(
        ticket_id=result.ticket.ticket_id,
        summary=AISummaryOut(
            text=result.summary.text,
            requester_id=result.summary.requester_id,
            title=result.summary.title,
            status=result.summary.status,
            priority=result.summary.priority,
            category=result.summary.category,
            queue=result.summary.queue,
            urgency_signals=list(result.summary.urgency_signals),
            history_count=result.summary.history_count,
        ),
        knowledge_base=[
            KnowledgeBaseMatchOut(
                article=_article_out(match.article), score=match.score
            )
            for match in result.knowledge_base
        ],
        recommendations=[
            AIRecommendationOut(
                id=recommendation.id,
                kind=recommendation.kind,
                title=recommendation.title,
                current=recommendation.current,
                suggested=recommendation.suggested,
                rationale=recommendation.rationale,
                confidence=recommendation.confidence,
                applicable=recommendation.applicable,
            )
            for recommendation in result.recommendations
        ],
        adapter=result.adapter,
        generated_at=db.utcnow(),
    )


def _analyze_ticket(
    record: db.TicketRecord,
    history: list[dict[str, Any]],
    *,
    request: Optional[Request] = None,
    session: Optional[Session] = None,
    actor: Optional[Actor] = None,
) -> ai_agent.TriageResult:
    snapshot = ai_agent.TicketSnapshot.from_record(record)
    try:
        return AI_ADAPTER.analyze(snapshot, history)
    except ai_agent.AIAvailabilityError as error:
        provider = _ai_provider_name()
        _record_ai_triage(provider, "error")
        _record_ai_provider_error(provider)
        if session is not None and actor is not None:
            db.log_execution(
                session,
                run_id=_new_run_id(),
                action="ai.error",
                status="failure",
                ticket_id=record.ticket_id,
                duration_ms=0,
                detail=ai_agent.redact_text(str(error), 500),
                actor_subject=actor.subject,
                correlation_id=getattr(request.state, "correlation_id", None)
                if request is not None
                else None,
            )
            session.commit()
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/tickets/{ticket_id}/ai-triage", response_model=AITriageOut)
def triage_ticket(
    ticket_id: str,
    request: Request,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> AITriageOut:
    """Run AI triage and return suggestions without changing ticket state."""
    record = _load_ticket(session, ticket_id)
    history = _ticket_history(session, ticket_id)
    started = time.perf_counter()
    result = _analyze_ticket(
        record, history, request=request, session=session, actor=actor
    )
    _record_ai_triage(result.adapter, "success")
    duration_ms = int((time.perf_counter() - started) * 1000)
    correlation_id = getattr(request.state, "correlation_id", None)
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action="ai.summarize",
        status="success",
        ticket_id=ticket_id,
        duration_ms=duration_ms,
        detail=f"adapter={result.adapter}; history={len(history)}",
        actor_subject=actor.subject,
        correlation_id=correlation_id,
    )
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action="ai.recommend",
        status="success",
        ticket_id=ticket_id,
        duration_ms=duration_ms,
        detail=(
            f"adapter={result.adapter}; recommendations={len(result.recommendations)}; "
            f"kb={len(result.knowledge_base)}"
        ),
        actor_subject=actor.subject,
        correlation_id=correlation_id,
    )
    session.commit()
    return _triage_out(result)


@app.post(
    "/tickets/{ticket_id}/ai-recommendations/apply",
    response_model=AIApplyOut,
)
def apply_ai_recommendations(
    ticket_id: str,
    payload: AIApplyRequest,
    request: Request,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> AIApplyOut:
    """Apply explicitly approved AI suggestions to queue/priority only.

    The server re-runs triage and applies only recommendation IDs the operator
    submitted. Informational suggestions and status changes are never applied.
    """
    record = _load_ticket(session, ticket_id)
    history = _ticket_history(session, ticket_id)
    started = time.perf_counter()
    result = _analyze_ticket(
        record, history, request=request, session=session, actor=actor
    )
    by_id = {recommendation.id: recommendation for recommendation in result.recommendations}
    unknown = [item for item in payload.recommendation_ids if item not in by_id]
    if unknown:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown AI recommendation id(s): {', '.join(unknown)}.",
        )

    applied: list[str] = []
    applied_fields: list[str] = []
    for recommendation in (by_id[item] for item in payload.recommendation_ids):
        if not recommendation.applicable or recommendation.id in applied:
            continue
        if recommendation.kind == "queue" and recommendation.suggested:
            record.queue = recommendation.suggested
            applied.append(recommendation.id)
            applied_fields.append(f"queue={recommendation.suggested}")
            _record_ai_apply("queue")
        elif recommendation.kind == "priority" and recommendation.suggested:
            record.priority = recommendation.suggested
            applied.append(recommendation.id)
            applied_fields.append(f"priority={recommendation.suggested}")
            _record_ai_apply("priority")

    if not applied:
        raise HTTPException(
            status_code=409,
            detail="No applicable AI recommendations were selected.",
        )

    session.flush()
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action="ai.recommendation_applied",
        status="success",
        ticket_id=ticket_id,
        duration_ms=int((time.perf_counter() - started) * 1000),
        detail=f"recommendations={','.join(applied)}; {'; '.join(applied_fields)}",
        actor_subject=actor.subject,
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    session.commit()
    return AIApplyOut(
        ticket=Ticket.model_validate(record, from_attributes=True),
        applied=applied,
    )


@app.get("/knowledge-base", response_model=list[KnowledgeBaseArticleOut])
def get_knowledge_base(
    query: Optional[str] = Query(default=None),
    category: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
) -> list[KnowledgeBaseArticleOut]:
    """List or search the simulated IT knowledge base."""
    if query:
        matches = ai_agent.search_knowledge_base(query, limit=limit)
        articles = [match.article for match in matches]
    else:
        articles = list(ai_agent.list_knowledge_base())
    if category:
        articles = [article for article in articles if article.category == category]
    return [_article_out(article) for article in articles]


@app.get("/tickets", response_model=list[Ticket])
def list_tickets(
    session: Session = Depends(db.get_session),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[Ticket]:
    """List stored tickets, ordered by numeric ticket ID."""
    records = session.execute(select(db.TicketRecord)).scalars().all()
    tickets = [Ticket.model_validate(record, from_attributes=True) for record in records]
    ordered = sorted(tickets, key=lambda ticket: int(ticket.ticket_id))
    return ordered[offset:offset + limit]


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
    actor_subject: str = Depends(_webhook_actor),
    session: Session = Depends(db.get_session),
) -> Any:
    """n8n-friendly intake: accepts one ticket or a list, always 200.

    Each item is validated, routed, stored, and logged exactly like
    POST /tickets. Invalid payloads are rejected with 422 before any
    item is processed.
    """
    if isinstance(payload, list):
        return [_process_intake(item, session, actor_subject) for item in payload]
    return _process_intake(payload, session, actor_subject)


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
    request: OnboardingRequestIn,
    session: Session,
    actor_subject: Optional[str] = None,
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
        actor_subject=actor_subject,
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
    actor: Actor = Depends(_operator_actor),
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



@app.get("/ui/tickets", response_class=HTMLResponse, include_in_schema=False)
def ui_tickets(
    request: Request, session: Session = Depends(db.get_session)
) -> HTMLResponse:
    """Operator ticket list with an AI-triage entry point."""
    records = session.execute(
        select(db.TicketRecord).order_by(db.TicketRecord.ticket_id)
    ).scalars().all()
    tickets = [Ticket.model_validate(row, from_attributes=True) for row in records]
    tickets.sort(key=lambda ticket: int(ticket.ticket_id))
    return web.render(
        request,
        "tickets.html",
        {"active": "tickets", "tickets": tickets},
    )


@app.get(
    "/ui/tickets/{ticket_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def ui_ticket_detail(
    request: Request,
    ticket_id: str,
    triage: bool = Query(default=False),
    session: Session = Depends(db.get_session),
) -> HTMLResponse:
    """Ticket detail page with an optional AI-triage panel."""
    record = _load_ticket(session, ticket_id)
    result: Optional[AITriageOut] = None
    if triage:
        history = _ticket_history(session, ticket_id)
        analysis = _analyze_ticket(record, history)
        correlation_id = getattr(request.state, "correlation_id", None)
        started = time.perf_counter()
        db.log_execution(
            session,
            run_id=_new_run_id(),
            action="ai.summarize",
            status="success",
            ticket_id=ticket_id,
            duration_ms=int((time.perf_counter() - started) * 1000),
            detail=f"adapter={analysis.adapter}; source=operator-ui",
            actor_subject="operator-ui",
            correlation_id=correlation_id,
        )
        db.log_execution(
            session,
            run_id=_new_run_id(),
            action="ai.recommend",
            status="success",
            ticket_id=ticket_id,
            duration_ms=int((time.perf_counter() - started) * 1000),
            detail=f"adapter={analysis.adapter}; source=operator-ui",
            actor_subject="operator-ui",
            correlation_id=correlation_id,
        )
        session.commit()
        result = _triage_out(analysis)
    return web.render(
        request,
        "ticket_detail.html",
        {"active": "tickets", "ticket": record, "triage": result},
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
    request: Request,
    session: Session = Depends(db.get_session),
    page: int = Query(default=1, ge=1),
) -> HTMLResponse:
    """Operator page for pending and dead-letter webhook deliveries."""
    page_size = 25
    records = session.execute(
        select(db.NotificationOutboxRecord)
        .order_by(db.NotificationOutboxRecord.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).scalars().all()
    return web.render(
        request,
        "notifications.html",
        {
            "active": "notifications",
            "pending": [item for item in records if item.status == "pending"],
            "failed": [item for item in records if item.status == "failed"],
            "page": page,
            "has_previous": page > 1,
            "has_next": len(records) == page_size,
        },
    )


@app.get("/ui/workflow-jobs", response_class=HTMLResponse, include_in_schema=False)
def ui_workflow_jobs(
    request: Request,
    session: Session = Depends(db.get_session),
    page: int = Query(default=1, ge=1),
) -> HTMLResponse:
    """Operator page for the durable workflow queue."""
    page_size = 25
    records = jobs.list_jobs(
        session, limit=page_size, offset=(page - 1) * page_size
    )
    return web.render(
        request,
        "jobs.html",
        {
            "active": "jobs",
            "jobs": records,
            "page": page,
            "has_previous": page > 1,
            "has_next": len(records) == page_size,
        },
    )


@app.get("/ui/audit", response_class=HTMLResponse, include_in_schema=False)
def ui_audit(
    request: Request,
    session: Session = Depends(db.get_session),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    actor_filter: Optional[str] = Query(default=None, alias="actor"),
    page: int = Query(default=1, ge=1),
) -> HTMLResponse:
    """Operator audit page for actor-attributed execution rows."""
    query = select(db.ExecutionLogRecord).where(
        db.ExecutionLogRecord.actor_subject.is_not(None)
    )
    if status_filter in ("success", "failure"):
        query = query.where(db.ExecutionLogRecord.status == status_filter)
    if actor_filter:
        query = query.where(db.ExecutionLogRecord.actor_subject == actor_filter)
    page_size = 25
    records = session.execute(
        query.order_by(db.ExecutionLogRecord.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).scalars().all()
    return web.render(
        request,
        "audit.html",
        {
            "active": "audit",
            "logs": records,
            "status_filter": status_filter,
            "actor_filter": actor_filter or "",
            "page": page,
            "has_previous": page > 1,
            "has_next": len(records) == page_size,
        },
    )


@app.get(
    "/ui/workflow-jobs/{job_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def ui_workflow_job_detail(
    request: Request, job_id: int, session: Session = Depends(db.get_session)
) -> HTMLResponse:
    """Operator detail page for one durable workflow job."""
    job = session.get(db.WorkflowJobRecord, job_id)
    if job is None:
        return web.render(request, "404.html", {"active": "jobs"}, status_code=404)
    try:
        payload = json.loads(job.payload_json)
    except json.JSONDecodeError:
        payload = {}
    try:
        result = json.loads(job.result_json) if job.result_json else {}
    except json.JSONDecodeError:
        result = {}
    return web.render(
        request,
        "job_detail.html",
        {
            "active": "jobs",
            "job": job,
            "payload": json.dumps(payload, indent=2, sort_keys=True),
            "result": json.dumps(result, indent=2, sort_keys=True),
        },
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
    actor_subject: Optional[str] = None,
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
        actor_subject=actor.subject,
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
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> OnboardingRequestOut:
    """Execute a checklist action through the configured Microsoft adapter."""
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
    request_id: int,
    item_id: int,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> OnboardingRequestOut:
    """Approve a checklist step that requires human sign-off."""
    return _transition_item(session, request_id, item_id, "approve", actor.subject)


@app.post("/onboarding-requests/{request_id}/items/{item_id}/complete")
def complete_item(
    request_id: int,
    item_id: int,
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
    if record.request_type == "offboarding":
        raise HTTPException(
            409,
            "Offboarding steps must be completed via "
            "POST /offboarding-requests/{checklist_id}/steps/{step_key}/complete "
            "(idempotent completion ledger).",
        )
    return _transition_item(
        session, request_id, item_id, "complete", actor.subject
    )



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
# Routes: durable workflow jobs


@app.post(
    "/onboarding-requests/{request_id}/items/{item_id}/jobs",
    response_model=WorkflowJobOut,
    status_code=http_status.HTTP_202_ACCEPTED,
)
def enqueue_onboarding_item_job(
    request_id: int,
    item_id: int,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> WorkflowJobOut:
    """Queue one onboarding checklist action for a durable worker."""
    record, _ = _load_request(session, request_id)
    item = _get_item(session, record, item_id)
    if item.status == "completed":
        raise HTTPException(409, "Checklist item is already completed.")
    required_status = "approved" if item.requires_approval else "pending"
    if item.status != required_status:
        raise HTTPException(
            409,
            f"Item must be '{required_status}' before execution "
            f"(current status: '{item.status}').",
        )
    action = action_for_step(item.step)
    job = jobs.enqueue_job(
        session,
        job_type=jobs.JOB_ONBOARDING_ITEM,
        payload={"request_id": record.id, "item_id": item.id, "action": action},
        idempotency_key=f"onboarding:{record.id}:{item.id}",
        resource_type="onboarding",
        resource_id=str(record.id),
        label=f"{record.employee_name}: {item.step}",
    )
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action="workflow.job.enqueued",
        status="success",
        ticket_id=str(record.id),
        duration_ms=0,
        detail=f"job #{job.id} ({action}) queued for item #{item.id}",
        actor_subject=actor.subject,
    )
    session.commit()
    return WorkflowJobOut.model_validate(job)


@app.get("/workflow-jobs", response_model=list[WorkflowJobOut])
def list_workflow_jobs(
    session: Session = Depends(db.get_session),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[WorkflowJobOut]:
    """List durable workflow jobs, newest first."""
    return [
        WorkflowJobOut.model_validate(job)
        for job in jobs.list_jobs(
            session, status=status_filter, limit=limit, offset=offset
        )
    ]


@app.get("/workflow-jobs/{job_id}", response_model=WorkflowJobOut)
def get_workflow_job(
    job_id: int, session: Session = Depends(db.get_session)
) -> WorkflowJobOut:
    """Return one durable workflow job."""
    job = session.get(db.WorkflowJobRecord, job_id)
    if job is None:
        raise HTTPException(404, "Workflow job not found.")
    return WorkflowJobOut.model_validate(job)


@app.post("/workflow-jobs/{job_id}/retry", response_model=WorkflowJobOut)
def retry_workflow_job(
    job_id: int,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> WorkflowJobOut:
    """Return a failed workflow job to the queue with a fresh attempt budget."""
    job = session.get(db.WorkflowJobRecord, job_id)
    if job is None:
        raise HTTPException(404, "Workflow job not found.")
    if job.status != "failed":
        raise HTTPException(
            409, f"Only failed jobs can be retried (current status: '{job.status}')."
        )
    if not jobs.retry_failed_job(session, job_id):
        raise HTTPException(409, "Workflow job changed state; refresh and retry.")
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action="workflow.job.retried",
        status="success",
        ticket_id=job.resource_id,
        duration_ms=0,
        detail=f"job #{job_id} returned to the queue",
        actor_subject=actor.subject,
    )
    session.commit()
    session.expire_all()
    job = session.get(db.WorkflowJobRecord, job_id)
    return WorkflowJobOut.model_validate(job)


@app.post("/workflow-jobs/{job_id}/cancel", response_model=WorkflowJobOut)
def cancel_workflow_job(
    job_id: int,
    actor: Actor = Depends(_admin_actor),
    session: Session = Depends(db.get_session),
) -> WorkflowJobOut:
    """Cancel a queued or retry-scheduled workflow job."""
    job = session.get(db.WorkflowJobRecord, job_id)
    if job is None:
        raise HTTPException(404, "Workflow job not found.")
    if not jobs.cancel_job(session, job_id):
        raise HTTPException(
            409, "Only queued or retry-scheduled jobs can be cancelled."
        )
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action="workflow.job.cancelled",
        status="success",
        ticket_id=job.resource_id,
        duration_ms=0,
        detail=f"job #{job_id} cancelled",
        actor_subject=actor.subject,
    )
    session.commit()
    session.expire_all()
    job = session.get(db.WorkflowJobRecord, job_id)
    return WorkflowJobOut.model_validate(job)


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
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[NotificationOutboxOut]:
    """List webhook deliveries that await a manual retry."""
    records = session.execute(
        select(db.NotificationOutboxRecord)
        .where(db.NotificationOutboxRecord.status == "pending")
        .order_by(db.NotificationOutboxRecord.id)
        .offset(offset)
        .limit(limit)
    ).scalars().all()
    return [_outbox_out(record) for record in records]


@app.get("/notifications/failed", response_model=list[NotificationOutboxOut])
def list_failed_notifications(
    session: Session = Depends(db.get_session),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[NotificationOutboxOut]:
    """List dead-letter deliveries that exhausted their retry budget.

    Operators can inspect these rows and return one to the pending queue with
    ``POST /notifications/{notification_id}/requeue``.
    """
    records = session.execute(
        select(db.NotificationOutboxRecord)
        .where(db.NotificationOutboxRecord.status == "failed")
        .order_by(db.NotificationOutboxRecord.id)
        .offset(offset)
        .limit(limit)
    ).scalars().all()
    return [_outbox_out(record) for record in records]


@app.post("/notifications/retry", response_model=list[NotificationOutboxOut])
def retry_failed_notifications(
    actor: Actor = Depends(_operator_actor),
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
                    actor_subject=actor.subject,
                )
        db.log_execution(
            session,
            run_id=_new_run_id(),
            action="notification.retry",
            status="success" if delivery.success else "failure",
            ticket_id=record.ticket_id,
            duration_ms=duration_ms,
            detail=f"outbox #{record.id}: now '{record.status}'",
            actor_subject=actor.subject,
        )
        results.append(_outbox_out(record))
    return results


@app.post(
    "/notifications/{notification_id}/requeue",
    response_model=NotificationOutboxOut,
)
def requeue_failed_notification(
    notification_id: int,
    actor: Actor = Depends(_admin_actor),
    session: Session = Depends(db.get_session),
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
        actor_subject=actor.subject,
    )
    return _outbox_out(record)


@app.get("/execution-logs", response_model=list[ExecutionLogOut])
def list_execution_logs(
    session: Session = Depends(db.get_session),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    actor_filter: Optional[str] = Query(default=None, alias="actor"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ExecutionLogOut]:
    """Recent workflow execution log entries, newest first."""
    statement = select(db.ExecutionLogRecord).order_by(
        db.ExecutionLogRecord.id.desc()
    ).offset(offset).limit(limit)
    if status_filter in ("success", "failure"):
        statement = statement.where(db.ExecutionLogRecord.status == status_filter)
    if actor_filter:
        statement = statement.where(
            db.ExecutionLogRecord.actor_subject == actor_filter
        )
    records = session.execute(statement).scalars().all()
    return [
        ExecutionLogOut(
            **{c: getattr(record, c) for c in (
                "id", "run_id", "action", "status", "ticket_id",
                "duration_ms", "detail", "actor_subject", "correlation_id", "created_at",
            )}
        )
        for record in records
    ]


@app.get("/audit", response_model=list[ExecutionLogOut])
def list_audit_trail(
    session: Session = Depends(db.get_session),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    actor_filter: Optional[str] = Query(default=None, alias="actor"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ExecutionLogOut]:
    """Recent execution rows that carry an authenticated actor."""
    query = select(db.ExecutionLogRecord).where(
        db.ExecutionLogRecord.actor_subject.is_not(None)
    )
    if status_filter in ("success", "failure"):
        query = query.where(db.ExecutionLogRecord.status == status_filter)
    if actor_filter:
        query = query.where(db.ExecutionLogRecord.actor_subject == actor_filter)
    records = session.execute(
        query.order_by(db.ExecutionLogRecord.id.desc()).offset(offset).limit(limit)
    ).scalars().all()
    return [
        ExecutionLogOut(
            **{c: getattr(record, c) for c in (
                "id", "run_id", "action", "status", "ticket_id",
                "duration_ms", "detail", "actor_subject", "correlation_id", "created_at",
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
            "actor_subject": log.actor_subject,
            "correlation_id": log.correlation_id,
            "duration_ms": log.duration_ms,
        }
        for log in logs[:50]
    ]
    offboarding_rows = []
    for onboarding_record in session.execute(
        select(db.OnboardingRequestRecord)
    ).scalars().all():
        if onboarding_record.request_type != "offboarding":
            continue
        checklist = session.execute(
            select(db.ChecklistItemRecord).where(
                db.ChecklistItemRecord.request_id == onboarding_record.id
            )
        ).scalars().all()
        total = len(checklist)
        completed = sum(1 for item in checklist if item.status == "completed")
        offboarding_rows.append(
            {
                "id": onboarding_record.id,
                "employee_name": onboarding_record.employee_name,
                "status": onboarding_record.status,
                "completed": completed,
                "total": total,
                "percent": round((completed / total) * 100) if total else 0,
            }
        )
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
            "offboarding_rows": offboarding_rows,
            "ai_stats": {
                "triage": sum(_AI_TRIAGE.values()),
                "applies": sum(_AI_APPLY.values()),
                "errors": sum(_AI_PROVIDER_ERRORS.values()),
            },
        },
    )
