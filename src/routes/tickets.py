"""Ticket intake, routing, manual tickets, and AI-analysis helpers (v0.2+).

The ticket pipeline (normalize -> classify -> prioritize -> queue), the
ticket routes including n8n-style bulk intake and manual ticket creation,
and the ticket/AI analysis helpers shared with the AI router. Response
models, status codes, and the fenced idempotency protocol are unchanged;
``scripts/dump_openapi.py`` + ``tests/openapi_snapshot.json`` keep the
OpenAPI surface byte-identical across this decomposition.
"""

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy import Integer, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src import ai_agent, db, idempotency
from src.api import (  # noqa: E402
    IDEMPOTENCY_SCOPE_TICKET,
    _complete_idempotency_key,
    _record_ai_provider_error,
    _record_ai_triage,
    _release_idempotency_key,
    _request_fingerprint,
    _reserve_idempotency_key,
    _validate_idempotency_key,
)
from src.auth import Actor
from src.deps import (
    _actor_tenant,
    _new_run_id,
    _operator_actor,
    _require_ticket_access,
    _scope_execution_log_query,
    _webhook_actor,
)
from src.notify import deliver_webhook
from src.routing import route_ticket
from src.schemas import (
    AIRecommendationOut,
    AISummaryOut,
    AITriageOut,
    KnowledgeBaseArticleOut,
    KnowledgeBaseMatchOut,
    ManualTicketIn,
    Ticket,
    TicketDetailOut,
    TicketHistoryOut,
    TicketIntakeRequest,
)
from src.ticket_processor import normalize_ticket

router = APIRouter()

# v0.7 AI boundary: the adapter behind suggest-only triage. Tests monkeypatch
# this module attribute with deterministic doubles.
AI_ADAPTER: ai_agent.AIAdapter = ai_agent.adapter_from_environment()


def _ai_provider_name() -> str:
    return str(getattr(AI_ADAPTER, "provider", getattr(AI_ADAPTER, "name", "unknown")))


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
    actor_subject: str | None = None,
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



@router.post("/tickets", response_model=Ticket, status_code=http_status.HTTP_201_CREATED)
def create_ticket(
    request: TicketIntakeRequest,
    response: Response,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
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


@router.post("/tickets/manual", response_model=Ticket, status_code=201)
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
    request: Request | None = None,
    session: Session | None = None,
    actor: Actor | None = None,
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


@router.get("/tickets", response_model=list[Ticket])
def list_tickets(
    session: Session = Depends(db.get_session),
    actor: Actor = Depends(_operator_actor),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    tenant: str | None = Query(default=None, max_length=64),
    search: str | None = Query(default=None, max_length=200),
    status: str | None = Query(default=None, max_length=64),
    priority: str | None = Query(default=None, max_length=64),
    queue: str | None = Query(default=None, max_length=128),
    category: str | None = Query(default=None, max_length=128),
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


@router.get("/command-center/summary", response_model=DashboardSummaryOut)
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
        pending_approvals=session.scalar(items.where(
            active, db.ChecklistItemRecord.requires_approval.is_(True), db.ChecklistItemRecord.status == "pending")) or 0,
        active_runs=session.scalar(runs.where(active)) or 0,
        overdue_steps=session.scalar(overdue) or 0,
        recorded_closed_today=session.scalar(tickets.where(
            db.TicketRecord.status == "closed",
            db.TicketRecord.closed_at >= midnight,
            db.TicketRecord.closed_at < midnight + timedelta(days=1))) or 0,
    )


@router.get("/tickets/{ticket_id}", response_model=TicketDetailOut)
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


@router.post("/webhooks/intake")
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


