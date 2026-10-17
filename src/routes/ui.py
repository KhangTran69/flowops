"""Server-rendered operator console pages (Jinja2)."""

import json
import secrets
import time
from collections import Counter
from typing import Optional

from fastapi import Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from src import chatops, db, definitions, jobs, scheduler, ui_session, web, workflow

from fastapi import APIRouter

from src import metrics as _metrics
from src.deps import _new_run_id
from src.api import (
    AITriageOut,
    Ticket,
    _analyze_ticket,
    _definition_out,
    _load_request,
    _load_ticket,
    _ticket_history,
    _triage_out,
    _workflow_run_out,
)

router = APIRouter()


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


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
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


@router.post("/login", response_class=HTMLResponse, include_in_schema=False)
def login_submit(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    next: str = Form(default=""),
    db_session: Session = Depends(db.get_session),
) -> Response:
    """Validate the admin username and password and create a signed browser session."""
    if not ui_session.ui_auth_required():
        return RedirectResponse("/dashboard", status_code=303)
    actor = ui_session.authenticate_admin(username.strip(), password)
    if actor is None:
        return web.render(
            request,
            "login.html",
            {
                "active": "login",
                "next": ui_session.safe_next(next),
                "error": "Invalid username or password.",
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


@router.post("/logout", response_class=HTMLResponse, include_in_schema=False)
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


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def root(
    request: Request, session: Session = Depends(db.get_session)
) -> HTMLResponse:
    """Landing page with live counts and operator links."""
    return web.render(
        request,
        "landing.html",
        {"active": "home", "stats": _site_stats(session)},
    )


@router.get("/ui/tickets", response_class=HTMLResponse, include_in_schema=False)
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


@router.get(
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


@router.get(
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


@router.get(
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
    run = scheduler.get_run_for_request(session, record.id)
    run_view = _workflow_run_out(session, run) if run is not None else None
    by_key = {workflow.state_key(item): item for item in items}
    item_views = []
    for item in sorted(items, key=lambda entry: entry.id):
        dependencies = workflow.dependencies_of(item)
        links = (
            chatops.approval_urls(record.id, item.id)
            if item.requires_approval and item.status == "pending"
            else {}
        )
        item_views.append(
            {
                "item": item,
                "depends_on_names": [
                    by_key[key].step for key in dependencies if key in by_key
                ],
                "missing": workflow.missing_prerequisites(item, items),
                "output": workflow.step_output(item),
                "chatops": links,
            }
        )
    return web.render(
        request,
        "onboarding_detail.html",
        {
            "active": "onboarding",
            "request_record": record,
            "item_views": item_views,
            "items": sorted(items, key=lambda item: item.id),
            "completed": completed,
            "total": len(items),
            "run": run_view,
        },
    )


@router.get("/ui/workflow-definitions", response_class=HTMLResponse, include_in_schema=False)
def ui_workflow_definitions(
    request: Request, session: Session = Depends(db.get_session)
) -> HTMLResponse:
    rows = [
        _definition_out(session, definition)
        for definition in definitions.list_definitions(session)
    ]
    return web.render(
        request, "workflow_definitions.html", {"active": "definitions", "definitions": rows}
    )


@router.get(
    "/ui/workflow-definitions/{key}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def ui_workflow_definition(
    request: Request, key: str, session: Session = Depends(db.get_session)
) -> HTMLResponse:
    definition = definitions.get_definition(session, key)
    if definition is None:
        return web.render(request, "404.html", {"active": "definitions"}, status_code=404)
    return web.render(
        request,
        "workflow_definition.html",
        {"active": "definitions", "definition": _definition_out(session, definition)},
    )


@router.get("/ui/workflow-runs", response_class=HTMLResponse, include_in_schema=False)
def ui_workflow_runs(
    request: Request,
    status: Optional[str] = Query(default=None),
    session: Session = Depends(db.get_session),
) -> HTMLResponse:
    """Operator view of DAG runs and their step attempts."""
    query = select(db.WorkflowRunRecord).order_by(db.WorkflowRunRecord.id.desc())
    if status:
        query = query.where(db.WorkflowRunRecord.status == status)
    runs = session.execute(query.limit(100)).scalars().all()
    rows = []
    for run in runs:
        record = session.get(db.OnboardingRequestRecord, run.request_id)
        rows.append(
            {
                "run": _workflow_run_out(session, run),
                "employee_name": record.employee_name if record else "—",
            }
        )
    return web.render(
        request,
        "workflow_runs.html",
        {"active": "workflow_runs", "runs": rows, "status_filter": status or ""},
    )


@router.get("/ui/notifications", response_class=HTMLResponse, include_in_schema=False)
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


@router.get("/ui/workflow-jobs", response_class=HTMLResponse, include_in_schema=False)
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


@router.get("/ui/audit", response_class=HTMLResponse, include_in_schema=False)
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


@router.get(
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


@router.get("/ui/{rest:path}", response_class=HTMLResponse, include_in_schema=False)
def ui_not_found(request: Request, rest: str) -> HTMLResponse:
    """Friendly HTML 404 for unknown operator pages."""
    return web.render(request, "404.html", {"active": ""}, status_code=404)


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
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
                "triage": sum(_metrics.snapshot_ai()[0].values()),
                "applies": sum(_metrics.snapshot_ai()[1].values()),
                "errors": sum(_metrics.snapshot_ai()[2].values()),
            },
        },
    )

