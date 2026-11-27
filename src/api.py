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

from src import chatops, db, definitions, idempotency, jobs, logging_config, metrics, scheduler, timers, ui_session, web, workflow, workflow_control
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
    _actor_tenant,
    _global_admin_actor,
    _new_run_id,
    _require_request_access,
    _require_workflow_job_access,
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
from src.routes import tickets as _tickets_routes  # noqa: E402
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
app.include_router(_tickets_routes.router)
app.include_router(_connector_routes.router)
app.include_router(_trigger_routes.router)
app.include_router(_workforce_routes.router)
app.include_router(_analytics_routes.router)
app.include_router(_monitoring_routes.router)

app.include_router(_ui_routes.router)
