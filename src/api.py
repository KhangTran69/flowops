"""FlowOps API — ticket intake, routing, onboarding workflows, and monitoring.

v0.3 adds database persistence and execution logs; v0.4 adds rule-based
routing, outbound notifications, and an n8n-friendly webhook; v0.5 adds
onboarding/offboarding checklists, idempotency keys, and delivery retries;
the v1.0 preview keeps failed webhook deliveries in a retryable outbox.
"""

import json
import os
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi import status as http_status
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import and_, delete, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src import ai_agent, chatops, db, definitions, idempotency, jobs, logging_config, metrics, scheduler, timers, ui_session, web, workflow, workflow_control
from src.actions import (
    ActionAdapter,
    action_for_step,
    adapter_from_environment,
)
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
    _new_run_id,
    _RATE_LIMIT_BUCKETS,  # noqa: F401  (re-exported for tests)
)
from src.schemas import (
    TicketIntakeRequest,
    Ticket,
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

ACTION_ADAPTER: ActionAdapter = adapter_from_environment()
AI_ADAPTER: ai_agent.AIAdapter = ai_agent.adapter_from_environment()
DEFAULT_ACTION_ATTEMPTS = 3
DEFAULT_NOTIFICATION_MAX_ATTEMPTS = 5
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
def metrics_endpoint(session: Session = Depends(db.get_session)) -> Response:
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

    job_records = session.execute(select(db.WorkflowJobRecord)).scalars().all()
    job_counts = Counter(job.status for job in job_records)
    lines.extend(
        [
            "flowops_workflow_jobs",
            "# TYPE flowops_workflow_jobs gauge",
        ]
    )
    for state in jobs.JOB_STATUSES:
        lines.append(f'flowops_workflow_jobs{{status="{state}"}} {job_counts.get(state, 0)}')
    lines.append("# HELP flowops_workflow_job_attempts_total Total workflow job attempts")
    lines.append("# TYPE flowops_workflow_job_attempts_total counter")
    lines.append(f"flowops_workflow_job_attempts_total {sum(job.attempts for job in job_records)}")

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
    runs = session.execute(select(db.WorkflowRunRecord)).scalars().all()
    run_counts = Counter(run.status for run in runs)
    lines.extend(
        [
            "# HELP flowops_dag_runs Workflow runs by status",
            "# TYPE flowops_dag_runs gauge",
        ]
    )
    for state in scheduler.RUN_STATUSES:
        lines.append(f'flowops_dag_runs{{status="{state}"}} {run_counts.get(state, 0)}')
    steps = session.execute(select(db.WorkflowStepRunRecord)).scalars().all()
    step_counts = Counter(step.status for step in steps)
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


@app.post("/webhooks/actions/approve")
def chatops_approve(
    request: Request,
    token: str = Query(...),
    source: str = Query(default="webhook"),
    session: Session = Depends(db.get_session),
) -> dict[str, Any]:
    """One-click ChatOps approval using a signed, single-use token."""
    return _chatops_action(request, session, token, "approve", source, None)


@app.post("/webhooks/actions/reject")
def chatops_reject(
    request: Request,
    token: str = Query(...),
    reason: Optional[str] = Query(default=None),
    source: str = Query(default="webhook"),
    session: Session = Depends(db.get_session),
) -> dict[str, Any]:
    """One-click ChatOps rejection with optional reason and cascade."""
    return _chatops_action(request, session, token, "reject", source, reason)


def _chatops_action(
    request: Request,
    session: Session,
    token: str,
    action: str,
    source: str,
    reason: Optional[str],
) -> dict[str, Any]:
    try:
        claims = chatops.verify_token(token)
    except chatops.ChatOpsUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except chatops.ChatOpsError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    if claims.action != action:
        raise HTTPException(status_code=401, detail="ChatOps token is for a different action.")
    record = session.get(db.OnboardingRequestRecord, claims.request_id)
    item = session.get(db.ChecklistItemRecord, claims.item_id)
    if record is None or item is None or item.request_id != record.id:
        raise HTTPException(status_code=404, detail="Checklist item not found.")
    if action == "approve" and item.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot approve an item with status '{item.status}'.",
        )
    if action == "reject" and item.status not in ("pending", "blocked", "approved"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot reject an item with status '{item.status}'.",
        )
    actor = f"chatops:{source if source in ('slack', 'teams', 'email') else 'webhook'}"
    try:
        session.add(
            db.ChatOpsTokenUseRecord(
                jti=claims.jti,
                request_id=record.id,
                item_id=item.id,
                action=action,
                actor_subject=actor,
            )
        )
        session.flush()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="ChatOps token has already been used.") from error

    if action == "approve":
        item.status = "approved"
        log_action = "onboarding.item.chatops_approved"
    else:
        item.status = "rejected"
        log_action = "onboarding.item.chatops_rejected"
    session.flush()
    items = session.execute(
        select(db.ChecklistItemRecord).where(
            db.ChecklistItemRecord.request_id == record.id
        )
    ).scalars().all()
    workflow.apply_graph_transitions(
        session,
        items,
        request_id=record.id,
        actor_subject=actor,
        reason=reason,
    )
    timers.sync_timers(session, record.id)
    db.log_execution(
        session,
        run_id=_new_run_id(),
        action=log_action,
        status="success",
        ticket_id=str(record.id),
        duration_ms=0,
        detail=(
            f"step '{item.step}' via {actor}; "
            f"status={item.status}; reason={reason or 'n/a'}"
        ),
        actor_subject=actor,
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    run = scheduler.get_run_for_request(session, record.id)
    if run is not None:
        scheduler.tick(session, run.id, worker_id="chatops", commit=False)
    session.commit()
    return {
        "request_id": record.id,
        "item_id": item.id,
        "action": action,
        "status": item.status,
        "actor": actor,
    }


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
    timer_rows = session.execute(
        select(db.WorkflowTimerRecord).where(
            db.WorkflowTimerRecord.request_id == request_id,
            db.WorkflowTimerRecord.status == "pending",
        )
    ).scalars().all()
    by_item = {row.item_id: row for row in timer_rows}
    now = db.utcnow()
    for item in items:
        timer = by_item.get(item.id)
        if timer is not None:
            due = timer.due_at
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            item.overdue = due <= now
            item.sla_remaining_minutes = max(
                0, int((due - now).total_seconds() // 60)
            )
        else:
            item.overdue = False
            item.sla_remaining_minutes = None
    return record, list(items)


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


@app.post("/workflow-definitions", response_model=WorkflowDefinitionOut)
def create_workflow_definition(
    payload: DefinitionCreateIn,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> WorkflowDefinitionOut:
    try:
        definition = definitions.create_definition(
            session,
            key=payload.key,
            name=payload.name,
            request_type=payload.request_type,
            steps=payload.steps,
            actor=actor.subject,
        )
    except definitions.DefinitionError as error:
        raise HTTPException(422, str(error)) from error
    session.commit()
    return _definition_out(session, definition)


@app.put("/workflow-definitions/{key}", response_model=WorkflowDefinitionOut)
def create_workflow_definition_version(
    key: str,
    payload: DefinitionVersionIn,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> WorkflowDefinitionOut:
    try:
        definitions.create_version(session, key, payload.steps, actor=actor.subject)
        definition = definitions.get_definition(session, key)
    except definitions.DefinitionError as error:
        raise HTTPException(422, str(error)) from error
    if definition is None:
        raise HTTPException(404, "Definition not found.")
    session.commit()
    return _definition_out(session, definition)


@app.post("/workflow-definitions/{key}/publish", response_model=WorkflowDefinitionOut)
def publish_workflow_definition(
    key: str,
    payload: DefinitionPublishIn,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> WorkflowDefinitionOut:
    try:
        definition = definitions.publish_version(
            session, key, payload.version, actor=actor.subject
        )
    except definitions.DefinitionError as error:
        raise HTTPException(422, str(error)) from error
    session.commit()
    return _definition_out(session, definition)


@app.post("/onboarding-requests/{request_id}/definition")
def migrate_definition(
    request_id: int,
    payload: DefinitionMigrationIn,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> dict[str, Any]:
    """Preview or apply a safe move to another definition version."""
    record, items = _load_request(session, request_id)
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


@app.get("/workflow-definitions", response_model=list[WorkflowDefinitionOut])
def list_workflow_definitions(
    session: Session = Depends(db.get_session),
) -> list[WorkflowDefinitionOut]:
    return [
        _definition_out(session, definition)
        for definition in definitions.list_definitions(session)
    ]


@app.get("/workflow-definitions/{key}", response_model=WorkflowDefinitionOut)
def get_workflow_definition(
    key: str, session: Session = Depends(db.get_session)
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


def _workflow_run_out(
    session: Session, run: db.WorkflowRunRecord
) -> WorkflowRunOut:
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
    return WorkflowRunOut(
        id=run.id,
        request_id=run.request_id,
        status=run.status,
        max_parallel=run.max_parallel,
        error=run.error,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
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


@app.post("/onboarding-requests/{request_id}/run", response_model=WorkflowRunOut)
def start_workflow_run(
    request_id: int,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> WorkflowRunOut:
    """Start the DAG orchestrator for a request (idempotent per request)."""
    _load_request(session, request_id)
    try:
        run = scheduler.start_run(session, request_id, actor_subject=actor.subject)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    session.commit()
    session.refresh(run)
    return _workflow_run_out(session, run)


@app.get("/onboarding-requests/{request_id}/run", response_model=WorkflowRunOut)
def get_workflow_run(
    request_id: int,
    session: Session = Depends(db.get_session),
) -> WorkflowRunOut:
    run = scheduler.get_run_for_request(session, request_id)
    if run is None:
        raise HTTPException(404, "Workflow run not found.")
    return _workflow_run_out(session, run)


@app.get("/workflow-runs", response_model=list[WorkflowRunOut])
def list_workflow_runs(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(db.get_session),
) -> list[WorkflowRunOut]:
    query = select(db.WorkflowRunRecord).order_by(db.WorkflowRunRecord.id.desc())
    if status_filter:
        query = query.where(db.WorkflowRunRecord.status == status_filter)
    runs = session.execute(query.offset(offset).limit(limit)).scalars().all()
    return [_workflow_run_out(session, run) for run in runs]


def _run_action(
    session: Session, run_id: int, actor: Actor, action: str
) -> WorkflowRunOut:
    try:
        if action == "pause":
            run = scheduler.pause_run(session, run_id, actor.subject)
        elif action == "resume":
            run = scheduler.resume_run(session, run_id, actor.subject)
        else:
            run = scheduler.cancel_run(session, run_id, actor.subject)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    session.commit()
    session.refresh(run)
    return _workflow_run_out(session, run)


@app.post("/workflow-runs/{run_id}/pause", response_model=WorkflowRunOut)
def pause_workflow_run(
    run_id: int,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> WorkflowRunOut:
    return _run_action(session, run_id, actor, "pause")


@app.post("/workflow-runs/{run_id}/resume", response_model=WorkflowRunOut)
def resume_workflow_run(
    run_id: int,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> WorkflowRunOut:
    return _run_action(session, run_id, actor, "resume")


@app.post("/workflow-runs/{run_id}/cancel", response_model=WorkflowRunOut)
def cancel_workflow_run(
    run_id: int,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> WorkflowRunOut:
    return _run_action(session, run_id, actor, "cancel")


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
    context = {
        "request_id": record.id,
        "request_type": record.request_type,
        "employee_name": record.employee_name,
        "employee_email": record.employee_email,
        "department": record.department,
        "actor_subject": actor.subject,
        "actor_roles": list(actor.roles),
        **workflow.action_context_outputs(item, items),
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
    request: Request,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> OnboardingRequestOut:
    """Approve a checklist step that requires human sign-off."""
    return _transition_item(
        session, request_id, item_id, request, "approve", actor.subject
    )


@app.post("/onboarding-requests/{request_id}/items/{item_id}/complete")
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


@app.get("/onboarding-requests/m365-status/{user_id}", response_model=M365StatusOut)
def get_m365_status(user_id: str) -> M365StatusOut:
    """Look up a *simulated* M365 provisioning status by user id.

    This never contacts real Microsoft services; see src/m365_simulator.py.
    """
    return M365StatusOut.model_validate(M365Simulator().get_status(user_id))


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


@app.get("/workflow-jobs/{job_id}/history", response_model=list[ActionExecutionOut])
def workflow_job_history(
    job_id: int, session: Session = Depends(db.get_session)
) -> list[ActionExecutionOut]:
    """Return every durable worker attempt for a workflow job."""
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


from src.routes import reports as _report_routes  # noqa: E402
from src.routes import tenants as _tenant_routes  # noqa: E402
from src.routes import ui as _ui_routes  # noqa: E402  (registered last: owns the /ui catch-all)

app.include_router(_tenant_routes.router)
app.include_router(_report_routes.router)

app.include_router(_ui_routes.router)
