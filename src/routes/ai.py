"""AI support agent and ChatOps routes (extracted from src/api.py).

Suggest-only AI triage with operator-approved apply, the knowledge-base
search, and the signed single-use ChatOps approve/reject webhooks. Route
bodies, response models, and error semantics are unchanged;
``scripts/dump_openapi.py`` + the committed ``tests/openapi_snapshot.json``
keep the OpenAPI surface byte-identical across this decomposition.
"""

import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src import ai_agent, chatops, db, scheduler, timers, workflow
from src.auth import Actor
from src.deps import _new_run_id, _operator_actor, _require_ticket_access
from src.schemas import (
    AIApplyOut,
    AIApplyRequest,
    AITriageOut,
    KnowledgeBaseArticleOut,
    Ticket,
)

from src.routes.tickets import (  # noqa: E402
    _analyze_ticket,
    _article_out,
    _load_ticket,
    _ticket_history,
    _triage_out,
)

from src.api import _record_ai_apply, _record_ai_triage  # noqa: E402


router = APIRouter()

@router.post("/tickets/{ticket_id}/ai-triage", response_model=AITriageOut)
def triage_ticket(
    ticket_id: str,
    request: Request,
    actor: Actor = Depends(_operator_actor),
    session: Session = Depends(db.get_session),
) -> AITriageOut:
    """Run AI triage and return suggestions without changing ticket state."""
    record = _load_ticket(session, ticket_id)
    _require_ticket_access(actor, record)
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


@router.post(
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
    _require_ticket_access(actor, record)
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


@router.get("/knowledge-base", response_model=list[KnowledgeBaseArticleOut])
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


@router.post("/webhooks/actions/approve")
def chatops_approve(
    request: Request,
    token: str = Query(...),
    source: str = Query(default="webhook"),
    session: Session = Depends(db.get_session),
) -> dict[str, Any]:
    """One-click ChatOps approval using a signed, single-use token."""
    return _chatops_action(request, session, token, "approve", source, None)


@router.post("/webhooks/actions/reject")
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
    if claims.tenant_key and record.tenant_key != claims.tenant_key:
        # The token binds one tenant; stale or re-signed links cannot cross it.
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


