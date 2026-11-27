"""FlowOps API — ticket intake, routing, onboarding workflows, and monitoring.

v0.3 adds database persistence and execution logs; v0.4 adds rule-based
routing, outbound notifications, and an n8n-friendly webhook; v0.5 adds
onboarding/offboarding checklists, idempotency keys, and delivery retries;
the v1.0 preview keeps failed webhook deliveries in a retryable outbox.
"""

import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Literal, Optional
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi import status as http_status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import Integer, String, and_, cast, delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src import ai_agent, chatops, db, definitions, idempotency, jobs, logging_config, metrics, scheduler, timers, ui_session, web, workflow, workflow_control
from src.connectors import deliver_outbox_notification
from src.actions import action_for_step
from src.auth import (
    Actor,
    validate_auth_configuration,
)
from src.m365_simulator import M365Simulator
from src.notify import deliver_webhook
from src.onboarding import checklist_for, initial_status_for
from src.routing import route_ticket
from src.ticket_processor import normalize_ticket
from src.deps import (
    _local_ui_actor,
    _actor_from_header,
    _operator_actor,
    _admin_actor,
    _webhook_actor,
    _actor_tenant as actor_tenant,
    _global_admin_actor,
    _new_run_id,
    validate_webhook_configuration,
    validate_workforce_integration_configuration,
    _RATE_LIMIT_BUCKETS,  # noqa: F401  (re-exported for tests)
)
from src.schemas import (
    TicketIntakeRequest,
    Ticket,
    TicketDetailOut,
    TicketHistoryOut,
    ManualTicketIn,
    KnowledgeBaseArticleOut,
    KnowledgeBaseMatchOut,
    AISummaryOut,
    AIRecommendationOut,
    AITriageOut,
    AIApplyRequest,
    AIApplyOut,
    WorkflowJobOut,
    ExecutionLogOut,
    ActionExecutionOut,
    ChecklistItemOut,
    DefinitionCreateIn,
    DefinitionVersionIn,
    DefinitionPublishIn,
    DefinitionMigrationIn,
    DefinitionVersionOut,
    WorkflowDefinitionOut,
    OnboardingRequestIn,
    OnboardingRequestOut,
    WorkflowStepRunOut,
    WorkflowEventOut,
    WorkflowRunOut,
    OnboardingStatusSummaryOut,
    OffboardingStepResultOut,
    OffboardingCompletionOut,
    M365StatusOut,
    NotificationOutboxOut,
)

AI_ADAPTER: ai_agent.AIAdapter = ai_agent.adapter_from_environment()
DEFAULT_ACTION_ATTEMPTS = 3
IDEMPOTENCY_PROCESSING_TIMEOUT = timedelta(minutes=5)
IDEMPOTENCY_SCOPE_TICKET = idempotency.SCOPE_TICKET
IDEMPOTENCY_SCOPE_ONBOARDING = idempotency.SCOPE_ONBOARDING
IDEMPOTENCY_REPLAY_ACTIONS = idempotency.REPLAY_ACTIONS


def _action_attempts(action: str) -> int:
    """Read a bounded per-action retry count from JSON configuration."""
    try:
        configured = json.loads(os.environ.get("FLOWOPS_ACTION_RETRIES", "{}"))
        return max(1, min(10, int(configured.get(action, DEFAULT_ACTION_ATTEMPTS))))
    except (TypeError, ValueError, json.JSONDecodeError):
        return DEFAULT_ACTION_ATTEMPTS


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


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Validate security configuration when the application starts."""
    validate_auth_configuration()
    ui_session.validate_ui_auth_configuration()
    validate_webhook_configuration()
    validate_workforce_integration_configuration()
    yield

app = FastAPI(
    title="FlowOps",
    description=(
        "IT workflow automation platform — ticket intake, routing, "
        "onboarding workflows, and monitoring. Learning prototype; "
        "ticket data is simulated, not real support tickets."
    ),
    version="1.0.0-preview",
    lifespan=_lifespan,
)


_SENSITIVE_FIELD = re.compile(r"secret|password|passwd|token|signature|api[_-]?key|authorization", re.I)


def _redact_sensitive(value):
    """Recursively replace values of sensitive-looking keys with a placeholder."""
    if isinstance(value, dict):
        return {
            k: ("[REDACTED]" if isinstance(k, str) and _SENSITIVE_FIELD.search(k) else _redact_sensitive(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive(v) for v in value]
    return value


@app.exception_handler(RequestValidationError)
async def _redacting_validation_handler(request: Request, exc: RequestValidationError):
    """422 handler that never echoes secrets back (FastAPI echoes raw input by default)."""
    errors = []
    for error in exc.errors():
        error = dict(error)
        loc = [str(part) for part in error.get("loc", ())]
        if any(_SENSITIVE_FIELD.search(part) for part in loc):
            error["input"] = "[REDACTED]"
        elif "input" in error:
            error["input"] = _redact_sensitive(error["input"])
        errors.append(error)
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})
app.mount(
    "/static",
    StaticFiles(directory=str(web.BASE_DIR / "static")),
    name="static",
)


def _ai_provider_name() -> str:
    return str(getattr(AI_ADAPTER, "provider", getattr(AI_ADAPTER, "name", "unknown")))


# In-process HTTP/AI counters live in src.metrics; these thin wrappers keep the
# call sites (and any monkeypatching in tests) stable.
_record_ai_triage = metrics.record_ai_triage
_record_ai_apply = metrics.record_ai_apply
_record_ai_provider_error = metrics.record_ai_provider_error
_record_request = metrics.record_request


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
    protected_ui_path = request.url.path in {"/", "/dashboard"} or request.url.path.startswith(
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
    route_path = getattr(route, "path", "<unmatched>")
    _record_request(request.method, route_path, response.status_code, duration)
    response.headers["X-Correlation-ID"] = correlation_id
    return response


@app.get("/auth/me")
def auth_me(actor: Actor = Depends(_actor_from_header)) -> dict:
    """Return the resolved local, bearer, or session actor."""
    return {
        "subject": actor.subject,
        "roles": list(actor.roles),
        "source": actor.source,
        "tenant_key": actor.tenant_key,
    }


# ---------------------------------------------------------------------------
# Idempotency protocol helpers. The implementation lives in src.idempotency so
# the ticket and onboarding routes share one fenced reservation protocol;
# these module-level aliases keep existing call sites and tests stable.

IDEMPOTENCY_PROCESSING_TIMEOUT = idempotency.PROCESSING_TIMEOUT
_fingerprint_payload = idempotency.fingerprint_payload
_request_fingerprint = idempotency.request_fingerprint
_validate_idempotency_key = idempotency.validate_idempotency_key
_stored_resource_type = idempotency._stored_resource_type
_claim_idempotency_key = idempotency.claim_key
_complete_idempotency_key = idempotency.complete_key
_release_idempotency_key = idempotency.release_key
_reservation_is_stale = idempotency._reservation_is_stale
_replay_stored_response = idempotency.replay_stored_response
_reserve_idempotency_key = idempotency.reserve_key


# ---------------------------------------------------------------------------
# Schemas


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
    actor = session.info.get("flowops_ticket_actor")
    if record is not None and actor is not None:
        # Recheck after lookup so a concurrent insert cannot bypass the route's
        # earlier ownership check and turn this upsert into a cross-tenant write.
        _require_ticket_access(actor, record)
    created = record is None
    if created:
        session.add(db.TicketRecord(**ticket, tenant_key=request.tenant))
    else:
        for field, value in ticket.items():
            setattr(record, field, value)
        if request.tenant:
            record.tenant_key = request.tenant
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
def metrics_endpoint(
    actor: Actor = Depends(_global_admin_actor),
    session: Session = Depends(db.get_session),
) -> Response:
    """Minimal Prometheus-style metrics for jobs, outbox, and HTTP requests."""
    lines = [
        "# HELP flowops_http_requests_total HTTP requests by method, route, and status",
        "# TYPE flowops_http_requests_total counter",
    ]
    counters = metrics.snapshot_http()
    for (method, route, status_code), count in sorted(counters.items()):
        lines.append(
            "flowops_http_requests_total"
            f'{{method="{method}",route="{route}",status="{status_code}"}} {count}'
        )

    lines.extend([
        "# HELP flowops_http_request_duration_seconds HTTP request latency in seconds",
        "# TYPE flowops_http_request_duration_seconds histogram",
    ])
    for (method, route), (counts, total, seconds) in sorted(metrics.snapshot_latency().items()):
        labels = f'method="{method}",route="{route}"'
        for upper, count in zip(metrics.LATENCY_BUCKETS, counts):
            lines.append(f'flowops_http_request_duration_seconds_bucket{{{labels},le="{upper}"}} {count}')
        lines.append(f'flowops_http_request_duration_seconds_bucket{{{labels},le="+Inf"}} {total}')
        lines.append(f'flowops_http_request_duration_seconds_count{{{labels}}} {total}')
        lines.append(f'flowops_http_request_duration_seconds_sum{{{labels}}} {seconds}')

    job_rows = session.execute(
        select(db.WorkflowJobRecord.status, func.count(), func.coalesce(func.sum(db.WorkflowJobRecord.attempts), 0))
        .group_by(db.WorkflowJobRecord.status)
    ).all()
    job_counts = {state: count for state, count, _ in job_rows}
    lines.extend(
        [
            "# HELP flowops_workflow_jobs Workflow jobs by state",
            "# TYPE flowops_workflow_jobs gauge",
        ]
    )
    for state in jobs.JOB_STATUSES:
        lines.append(f'flowops_workflow_jobs{{status="{state}"}} {job_counts.get(state, 0)}')
    lines.append("# HELP flowops_workflow_job_attempts_total Total workflow job attempts")
    lines.append("# TYPE flowops_workflow_job_attempts_total counter")
    lines.append(f"flowops_workflow_job_attempts_total {sum(attempts for _, _, attempts in job_rows)}")

    outbox_counts = dict(session.execute(
        select(db.NotificationOutboxRecord.status, func.count()).group_by(db.NotificationOutboxRecord.status)
    ).all())
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
    triage_counts, apply_counts, provider_errors = metrics.snapshot_ai()
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
    run_counts = dict(session.execute(
        select(db.WorkflowRunRecord.status, func.count()).group_by(db.WorkflowRunRecord.status)
    ).all())
    lines.extend(
        [
            "# HELP flowops_dag_runs Workflow runs by status",
            "# TYPE flowops_dag_runs gauge",
        ]
    )
    for state in scheduler.RUN_STATUSES:
        lines.append(f'flowops_dag_runs{{status="{state}"}} {run_counts.get(state, 0)}')
    step_counts = dict(session.execute(
        select(db.WorkflowStepRunRecord.status, func.count()).group_by(db.WorkflowStepRunRecord.status)
    ).all())
    lines.extend(
        [
            "# HELP flowops_dag_steps Workflow step attempts by status",
            "# TYPE flowops_dag_steps gauge",
        ]
    )
    for state in ("scheduled", "running", "succeeded", "failed", "skipped", "cancelled"):
        lines.append(f'flowops_dag_steps{{status="{state}"}} {step_counts.get(state, 0)}')
    lines.extend(
        [
            "# HELP flowops_dag_scheduler_ticks_total Scheduler ticks by outcome",
            "# TYPE flowops_dag_scheduler_ticks_total counter",
        ]
    )
    for outcome, count in sorted(scheduler.TICK_COUNTS.items()):
        lines.append(f'flowops_dag_scheduler_ticks_total{{outcome="{outcome}"}} {count}')
    lines.append(f"flowops_uptime_seconds {metrics.uptime_seconds():.1f}")
    lines.extend(_trigger_routes.trigger_metrics_lines(session))
    return Response("\n".join(lines) + "\n", media_type="text/plain")


# ---------------------------------------------------------------------------
# Routes: tickets


def _actor_tenant(actor: Actor) -> Optional[str]:
    """Compatibility wrapper for the shared actor-to-tenant policy."""
    return actor_tenant(actor)


def _require_ticket_access(actor: Actor, record: db.TicketRecord) -> None:
    """404 on cross-tenant reads so existence outside the tenant is not leaked."""
    tenant_key = _actor_tenant(actor)
    if tenant_key and record.tenant_key != tenant_key:
        raise HTTPException(status_code=404, detail="Ticket not found.")


def _scope_ticket_or_request_keyed_query(query, key_column, actor: Actor):
    """Limit rows keyed by a ticket or onboarding request to the actor's tenant."""
    tenant_key = _actor_tenant(actor)
    if tenant_key is None:
        return query
    foreign_request_ids = select(cast(db.OnboardingRequestRecord.id, String)).where(
        or_(
            db.OnboardingRequestRecord.tenant_key != tenant_key,
            db.OnboardingRequestRecord.tenant_key.is_(None),
        )
    )
    foreign_ticket_ids = select(db.TicketRecord.ticket_id).where(
        or_(
            db.TicketRecord.tenant_key != tenant_key,
            db.TicketRecord.tenant_key.is_(None),
        )
    )
    ticket_ids = select(db.TicketRecord.ticket_id).where(
        db.TicketRecord.tenant_key == tenant_key
    ).where(db.TicketRecord.ticket_id.not_in(foreign_request_ids))
    request_ids = select(cast(db.OnboardingRequestRecord.id, String)).where(
        db.OnboardingRequestRecord.tenant_key == tenant_key
    ).where(cast(db.OnboardingRequestRecord.id, String).not_in(foreign_ticket_ids))
    return query.where(key_column.in_(ticket_ids) | key_column.in_(request_ids))


def _scope_execution_log_query(query, actor: Actor):
    """Scope linked logs by tenant and retain only the actor's unlinked audit events."""
    tenant_key = _actor_tenant(actor)
    if tenant_key is None:
        return query
    linked_scope = _scope_ticket_or_request_keyed_query(
        select(db.ExecutionLogRecord.id), db.ExecutionLogRecord.ticket_id, actor
    ).subquery()
    return query.where(
        or_(
            db.ExecutionLogRecord.id.in_(select(linked_scope.c.id)),
            and_(
                db.ExecutionLogRecord.ticket_id.is_(None),
                db.ExecutionLogRecord.actor_subject == actor.subject,
            ),
        )
    )


def _require_request_access(actor: Actor, record: db.OnboardingRequestRecord) -> None:
    tenant_key = _actor_tenant(actor)
    if tenant_key and record.tenant_key != tenant_key:
        raise HTTPException(status_code=404, detail="Onboarding request not found.")


def _require_workflow_run_access(
    session: Session, actor: Actor, run: db.WorkflowRunRecord
) -> None:
    request = session.get(db.OnboardingRequestRecord, run.request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Workflow run not found.")
    _require_request_access(actor, request)


def _require_workflow_job_access(
    session: Session, actor: Actor, job: db.WorkflowJobRecord
) -> None:
    tenant_key = _actor_tenant(actor)
    if tenant_key is None:
        return
    if job.resource_type != "onboarding" or not job.resource_id or not job.resource_id.isdecimal():
        raise HTTPException(status_code=404, detail="Workflow job not found.")
    request = session.get(db.OnboardingRequestRecord, int(job.resource_id))
    if request is None or request.tenant_key != tenant_key:
        raise HTTPException(status_code=404, detail="Workflow job not found.")


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
    tenant_key = _actor_tenant(actor)
    if tenant_key:
        if request.tenant and request.tenant != tenant_key:
            raise HTTPException(status_code=404, detail="Ticket not found.")
        request.tenant = tenant_key
    existing = session.get(db.TicketRecord, str(request.id))
    if existing is not None:
        _require_ticket_access(actor, existing)
    _validate_idempotency_key(idempotency_key)
    request_hash = _request_fingerprint(request)
    owner_token, replay = _reserve_idempotency_key(
        session,
        response,
        idempotency_key,
        request_hash,
        IDEMPOTENCY_SCOPE_TICKET,
        str(request.id),
        actor_subject=actor.subject,
    )
    if replay is not None:
        result = Ticket.model_validate(replay["ticket"])
        record = session.get(db.TicketRecord, result.ticket_id)
        if record is None:
            raise HTTPException(404, "Ticket not found.")
        _require_ticket_access(actor, record)
        return result

    ownership_lost = False
    try:
        if existing is not None:
            response.status_code = http_status.HTTP_200_OK
        session.info["flowops_ticket_actor"] = actor
        try:
            result = _process_intake(request, session, actor.subject)
        finally:
            session.info.pop("flowops_ticket_actor", None)
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


@app.post("/tickets/manual", response_model=Ticket, status_code=201)
def create_manual_ticket(
    payload: ManualTicketIn,
    response: Response,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
) -> Ticket:
    """Create, never upsert, an operator-entered ticket in a separate ID namespace."""
    tenant = _actor_tenant(actor)
    # Positive integer IDs belong to the upstream intake contract. Negative
    # integer IDs are reserved for local creation and remain sortable by the
    # existing Jinja and API ticket lists.
    scope = idempotency.SCOPE_MANUAL_TICKET
    key = "manual:" + hashlib.sha256(
        json.dumps([tenant, actor.subject, idempotency_key], separators=(",", ":")).encode()
    ).hexdigest()
    fingerprint = _request_fingerprint(payload)
    owner, replay = _reserve_idempotency_key(
        session, response, key, fingerprint, scope, "manual", actor_subject=actor.subject
    )
    if replay is not None:
        result = Ticket.model_validate(replay["ticket"])
        existing = session.get(db.TicketRecord, result.ticket_id)
        if existing is None:
            raise HTTPException(404, "Ticket not found.")
        _require_ticket_access(actor, existing)
        return result
    if owner is None:
        raise HTTPException(409, "Unable to reserve manual ticket.")
    try:
        for _ in range(10):
            ticket_id = str(-(uuid.uuid4().int % 2147483647 + 1))
            if session.get(db.TicketRecord, ticket_id) is None:
                break
        else:
            raise HTTPException(503, "Unable to allocate a ticket ID; retry.")
        decision = route_ticket(payload.title)
        ticket = Ticket(
            ticket_id=ticket_id, requester_id=payload.requester_id,
            title=payload.title, status="open", priority=decision.priority,
            category=decision.category, queue=decision.queue,
        )
        session.add(db.TicketRecord(
            **ticket.model_dump(), tenant_key=tenant, description=payload.description,
            requester_email=payload.requester_email,
        ))
        session.flush()
        run_id = _new_run_id()
        db.log_execution(
            session, run_id=run_id, action="ticket.created", status="success",
            ticket_id=ticket_id, duration_ms=0,
            detail=f"manual intake routed to {decision.queue}", actor_subject=actor.subject,
        )
        _notify("ticket.created", ticket, run_id, session)
        if not _complete_idempotency_key(
            session, key, owner, fingerprint, scope, ticket_id,
            {"ticket": ticket.model_dump()},
        ):
            raise HTTPException(409, "Ticket reservation changed; retry.")
        return ticket
    except IntegrityError as error:
        session.rollback()
        _release_idempotency_key(session, key, owner)
        raise HTTPException(409, "Ticket ID allocation conflicted; retry.") from error
    except Exception:
        session.rollback()
        _release_idempotency_key(session, key, owner)
        raise


# ---------------------------------------------------------------------------
# Routes: AI triage and knowledge base


def _load_ticket(session: Session, ticket_id: str) -> db.TicketRecord:
    if not re.fullmatch(r"-?[0-9]+", ticket_id):
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


@app.get("/tickets", response_model=list[Ticket])
def list_tickets(
    session: Session = Depends(db.get_session),
    actor: Actor = Depends(_operator_actor),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    tenant: Optional[str] = Query(default=None, max_length=64),
    search: Optional[str] = Query(default=None, max_length=200),
    status: Optional[str] = Query(default=None, max_length=64),
    priority: Optional[str] = Query(default=None, max_length=64),
    queue: Optional[str] = Query(default=None, max_length=128),
    category: Optional[str] = Query(default=None, max_length=128),
) -> list[Ticket]:
    """List stored tickets with bounded database pagination."""
    query = select(db.TicketRecord).order_by(cast(db.TicketRecord.ticket_id, Integer))
    actor_tenant = _actor_tenant(actor)
    if actor_tenant is not None:
        if tenant is not None and tenant != actor_tenant:
            raise HTTPException(status_code=404, detail="Tenant not found.")
        query = query.where(db.TicketRecord.tenant_key == actor_tenant)
    elif tenant is not None:
        query = query.where(db.TicketRecord.tenant_key == tenant)
    for column, value in (
        (db.TicketRecord.status, status),
        (db.TicketRecord.priority, priority),
        (db.TicketRecord.queue, queue),
        (db.TicketRecord.category, category),
    ):
        if value:
            query = query.where(column == value)
    if search:
        query = query.where(or_(
            db.TicketRecord.title.icontains(search, autoescape=True),
            db.TicketRecord.queue.icontains(search, autoescape=True),
        ))
    records = session.execute(query.offset(offset).limit(limit)).scalars().all()
    return [Ticket.model_validate(record, from_attributes=True) for record in records]


class DashboardSummaryOut(BaseModel):
    open_tickets: int
    urgent_tickets: int
    pending_approvals: int
    active_runs: int
    overdue_steps: int
    recorded_closed_today: int


@app.get("/command-center/summary", response_model=DashboardSummaryOut)
def command_center_summary(
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> DashboardSummaryOut:
    tenant = _actor_tenant(actor)
    tickets = select(func.count()).select_from(db.TicketRecord)
    runs = select(func.count()).select_from(db.WorkflowRunRecord).join(
        db.OnboardingRequestRecord, db.OnboardingRequestRecord.id == db.WorkflowRunRecord.request_id
    )
    items = select(func.count()).select_from(db.ChecklistItemRecord).join(
        db.OnboardingRequestRecord, db.OnboardingRequestRecord.id == db.ChecklistItemRecord.request_id
    ).join(db.WorkflowRunRecord, db.WorkflowRunRecord.request_id == db.OnboardingRequestRecord.id)
    if tenant is not None:
        tickets = tickets.where(db.TicketRecord.tenant_key == tenant)
        runs = runs.where(db.OnboardingRequestRecord.tenant_key == tenant)
        items = items.where(db.OnboardingRequestRecord.tenant_key == tenant)
    active = db.WorkflowRunRecord.status.in_(("pending", "running"))
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    overdue = items.where(active, db.ChecklistItemRecord.status.in_(("pending", "approved"))).where(
        select(db.WorkflowTimerRecord.id).where(
            db.WorkflowTimerRecord.item_id == db.ChecklistItemRecord.id,
            db.WorkflowTimerRecord.request_id == db.ChecklistItemRecord.request_id,
            db.WorkflowTimerRecord.status == "pending",
            db.WorkflowTimerRecord.due_at <= now,
        ).exists()
    )
    return DashboardSummaryOut(
        open_tickets=session.scalar(tickets.where(db.TicketRecord.status == "open")) or 0,
        urgent_tickets=session.scalar(tickets.where(db.TicketRecord.status == "open", db.TicketRecord.priority == "urgent")) or 0,
        pending_approvals=session.scalar(items.where(active, db.ChecklistItemRecord.requires_approval.is_(True), db.ChecklistItemRecord.status == "pending")) or 0,
        active_runs=session.scalar(runs.where(active)) or 0,
        overdue_steps=session.scalar(overdue) or 0,
        recorded_closed_today=session.scalar(tickets.where(db.TicketRecord.status == "closed", db.TicketRecord.closed_at >= midnight, db.TicketRecord.closed_at < midnight + timedelta(days=1))) or 0,
    )


@app.get("/tickets/{ticket_id}", response_model=TicketDetailOut)
def get_ticket(
    ticket_id: str,
    session: Session = Depends(db.get_session),
    actor: Actor = Depends(_operator_actor),
) -> TicketDetailOut:
    """Return one stored ticket."""
    if not re.fullmatch(r"-?[0-9]+", ticket_id):
        raise HTTPException(status_code=404, detail="Ticket not found.")
    record = session.get(db.TicketRecord, ticket_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    _require_ticket_access(actor, record)
    detail = TicketDetailOut.model_validate(record, from_attributes=True)
    query = select(db.ExecutionLogRecord).where(
        db.ExecutionLogRecord.ticket_id == ticket_id,
        or_(db.ExecutionLogRecord.action.like("ticket.%"), db.ExecutionLogRecord.action.like("ai.%")),
    )
    query = _scope_execution_log_query(query, actor)
    history = session.execute(query.order_by(db.ExecutionLogRecord.id.desc()).limit(50)).scalars().all()
    detail.history = [TicketHistoryOut.model_validate(row, from_attributes=True) for row in history]
    return detail


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
# Routes: durable workflow jobs


@app.post(
    "/onboarding-requests/{request_id}/items/{item_id}/jobs",
    response_model=WorkflowJobOut,
    status_code=http_status.HTTP_202_ACCEPTED,
)
def enqueue_onboarding_item_job(
    request_id: int,
    item_id: int,
    request: Request,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> WorkflowJobOut:
    """Queue one onboarding checklist action for a durable worker."""
    record, items = _load_request(session, request_id)
    _require_request_access(actor, record)
    item = _get_item(session, record, item_id)
    if item.status == "completed":
        raise HTTPException(409, "Checklist item is already completed.")
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
    job = workflow_control.enqueue_onboarding_item(
        session,
        request_id=record.id,
        item_id=item.id,
        action=action,
        label=f"{record.employee_name}: {item.step}",
        actor_subject=actor.subject,
        correlation=getattr(request.state, "correlation_id", None),
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
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    session.commit()
    return WorkflowJobOut.model_validate(job)


@app.get("/workflow-jobs", response_model=list[WorkflowJobOut])
def list_workflow_jobs(
    session: Session = Depends(db.get_session),
    actor: Actor = Depends(_operator_actor),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[WorkflowJobOut]:
    """List durable workflow jobs, newest first."""
    tenant_key = _actor_tenant(actor)
    if tenant_key is not None:
        request_ids = select(cast(db.OnboardingRequestRecord.id, String)).where(
            db.OnboardingRequestRecord.tenant_key == tenant_key
        )
        query = select(db.WorkflowJobRecord).where(
            db.WorkflowJobRecord.resource_type == "onboarding",
            db.WorkflowJobRecord.resource_id.in_(request_ids),
        )
        if status_filter is not None:
            query = query.where(db.WorkflowJobRecord.status == status_filter)
        records = session.scalars(
            query.order_by(db.WorkflowJobRecord.id.desc()).offset(offset).limit(limit)
        ).all()
    else:
        records = jobs.list_jobs(session, status=status_filter, limit=limit, offset=offset)
    return [
        WorkflowJobOut.model_validate(job)
        for job in records
    ]


@app.get("/workflow-jobs/{job_id}", response_model=WorkflowJobOut)
def get_workflow_job(
    job_id: int, actor: Actor = Depends(_operator_actor), session: Session = Depends(db.get_session)
) -> WorkflowJobOut:
    """Return one durable workflow job."""
    job = session.get(db.WorkflowJobRecord, job_id)
    if job is None:
        raise HTTPException(404, "Workflow job not found.")
    _require_workflow_job_access(session, actor, job)
    return WorkflowJobOut.model_validate(job)


@app.get("/workflow-jobs/{job_id}/history", response_model=list[ActionExecutionOut])
def workflow_job_history(
    job_id: int, actor: Actor = Depends(_operator_actor), session: Session = Depends(db.get_session)
) -> list[ActionExecutionOut]:
    """Return every durable worker attempt for a workflow job."""
    job = session.get(db.WorkflowJobRecord, job_id)
    if job is None:
        raise HTTPException(404, "Workflow job not found.")
    _require_workflow_job_access(session, actor, job)
    try:
        records = workflow_control.history(session, job_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    return [ActionExecutionOut.model_validate(record, from_attributes=True) for record in records]
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
    _require_workflow_job_access(session, actor, job)
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
    _require_workflow_job_access(session, actor, job)
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


from src.routes import analytics as _analytics_routes  # noqa: E402  (v1.5)
from src.routes import ai as _ai_routes  # noqa: E402
from src.routes import onboarding as _onboarding_routes  # noqa: E402
from src.routes.onboarding import _get_item, _load_request  # noqa: E402  (used by durable-job routes below)
from src.routes import connectors as _connector_routes  # noqa: E402  (v1.4)
from src.routes import monitoring as _monitoring_routes  # noqa: E402
from src.routes import reports as _report_routes  # noqa: E402
from src.routes import tenants as _tenant_routes  # noqa: E402
from src.routes import triggers as _trigger_routes  # noqa: E402
from src.routes import ui as _ui_routes  # noqa: E402  (registered last: owns the /ui catch-all)
from src.routes import workforce as _workforce_routes  # noqa: E402

app.include_router(_tenant_routes.router)
app.include_router(_report_routes.router)
app.include_router(_ai_routes.router)
app.include_router(_onboarding_routes.router)
app.include_router(_connector_routes.router)
app.include_router(_trigger_routes.router)
app.include_router(_workforce_routes.router)
app.include_router(_analytics_routes.router)
app.include_router(_monitoring_routes.router)

app.include_router(_ui_routes.router)
