"""Shared FastAPI dependencies: actors, roles, CSRF, rate limiting."""

import os
import secrets
import threading
import time
import uuid
from dataclasses import replace

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import String, and_, cast, or_, select
from sqlalchemy.orm import Session

from src import db, ui_session
from src.auth import (
    Actor,
    AuthenticationError,
    authenticate_bearer,
)


def _local_ui_actor() -> Actor:
    return Actor(
        subject="local-development",
        roles=("automation.operator", "automation.admin"),
        source="local",
    )


def _actor_from_header(
    request: Request,
    authorization: str | None = Header(default=None),
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


def _enforce_csrf(request: Request | None, actor: Actor) -> None:
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
    _actor_tenant(actor)
    _enforce_csrf(request, actor)
    _enforce_rate_limit(f"actor:{actor.subject}")
    return actor


def _admin_actor(
    request: Request,
    actor: Actor = Depends(_actor_from_header),
) -> Actor:
    if "automation.admin" not in actor.roles:
        raise HTTPException(status_code=403, detail="Admin role is required.")
    _actor_tenant(actor)
    _enforce_csrf(request, actor)
    _enforce_rate_limit(f"actor:{actor.subject}")
    return actor


def _global_admin_actor(actor: Actor = Depends(_admin_actor)) -> Actor:
    """Require an unscoped admin for endpoints that expose cross-tenant data."""
    if actor.source != "local" and actor.tenant_key is not None:
        raise HTTPException(status_code=403, detail="Global administrator access is required.")
    return actor


def _actor_tenant(actor: Actor) -> str | None:
    """Return the actor's tenant, allowing global access only in dev or for admins."""
    if actor.source == "local":
        return None
    if actor.tenant_key is not None:
        tenant_key = actor.tenant_key.strip()
        if not tenant_key:
            raise HTTPException(
                status_code=403,
                detail="A non-empty tenant context is required.",
            )
        return tenant_key
    if "automation.admin" in actor.roles:
        return None
    raise HTTPException(
        status_code=403,
        detail="A tenant context or global administrator role is required.",
    )


def _webhook_actor(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> str:
    expected = os.environ.get("FLOWOPS_WEBHOOK_API_KEY")
    environment = os.environ.get("FLOWOPS_ENVIRONMENT", "development").strip().lower()
    if not expected and environment != "development":
        raise HTTPException(status_code=503, detail="Webhook authentication is not configured.")
    if expected and environment != "development" and len(expected) < 32:
        raise HTTPException(status_code=503, detail="Webhook authentication is not securely configured.")
    if expected and not secrets.compare_digest(x_api_key or "", expected):
        raise HTTPException(status_code=401, detail="Invalid webhook API key.")
    _enforce_rate_limit("webhook:intake")
    return "webhook-api-key" if expected else "local-development"


def _event_webhook_actor(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    x_tenant_key: str | None = Header(default=None, alias="X-Tenant-Key"),
    session=Depends(db.get_session),
) -> Actor:
    """Authenticate event ingress as either global or a tenant-bound identity."""
    environment = os.environ.get("FLOWOPS_ENVIRONMENT", "development").strip().lower()
    if x_tenant_key:
        tenant = session.get(db.TenantRecord, x_tenant_key)
        if tenant is None or not tenant.active or not tenant.credential_ref:
            raise HTTPException(status_code=401, detail="Invalid tenant webhook identity.")
        reference = tenant.credential_ref
        if not reference.isascii() or not reference.replace("_", "").isalnum():
            raise HTTPException(status_code=503, detail="Tenant webhook credentials are not configured safely.")
        expected = os.environ.get(f"FLOWOPS_TENANT_{reference.upper()}_WEBHOOK_API_KEY", "")
        if not expected or (environment != "development" and len(expected) < 32):
            raise HTTPException(status_code=503, detail="Tenant webhook authentication is not configured.")
        if not secrets.compare_digest(x_api_key or "", expected):
            raise HTTPException(status_code=401, detail="Invalid tenant webhook API key.")
        if secrets.compare_digest(
            expected, os.environ.get("FLOWOPS_WEBHOOK_API_KEY", "")
        ):
            raise HTTPException(status_code=503, detail="Tenant webhook key must be distinct from the global key.")
        for other in session.query(db.TenantRecord).filter(
            db.TenantRecord.active.is_(True)
        ):
            if other.key == tenant.key or not other.credential_ref:
                continue
            other_key = os.environ.get(
                f"FLOWOPS_TENANT_{other.credential_ref.upper()}_WEBHOOK_API_KEY", ""
            )
            if other_key and secrets.compare_digest(expected, other_key):
                raise HTTPException(status_code=503, detail="Tenant webhook keys must be unique per tenant.")
        subject = f"tenant-webhook:{tenant.key}"
        _enforce_rate_limit(f"webhook:{subject}")
        return Actor(subject=subject, roles=("automation.operator",), source="webhook", tenant_key=tenant.key)

    expected = os.environ.get("FLOWOPS_WEBHOOK_API_KEY", "")
    if not expected and environment != "development":
        raise HTTPException(status_code=503, detail="Webhook authentication is not configured.")
    if expected and environment != "development" and len(expected) < 32:
        raise HTTPException(status_code=503, detail="Webhook authentication is not securely configured.")
    if expected and not secrets.compare_digest(x_api_key or "", expected):
        raise HTTPException(status_code=401, detail="Invalid webhook API key.")
    if not expected and x_api_key:
        raise HTTPException(status_code=401, detail="Invalid webhook API key.")
    _enforce_rate_limit("webhook:events:global")
    return Actor(subject="webhook-api-key" if expected else "local-development",
                 roles=("automation.operator", "automation.admin"), source="webhook")


def validate_webhook_configuration(environ: dict[str, str] | None = None) -> None:
    """Fail startup when non-development webhook intake has no shared key."""
    values = environ if environ is not None else os.environ
    environment = values.get("FLOWOPS_ENVIRONMENT", "development").strip().lower()
    webhook_key = values.get("FLOWOPS_WEBHOOK_API_KEY", "")
    if environment != "development":
        if not webhook_key:
            raise ValueError("FLOWOPS_WEBHOOK_API_KEY is required outside development.")
        if len(webhook_key) < 32:
            raise ValueError(
                "FLOWOPS_WEBHOOK_API_KEY must contain at least 32 characters outside development."
            )


def validate_workforce_integration_configuration(
    environ: dict[str, str] | None = None,
) -> None:
    """Reject short configured workforce integration secrets outside development."""
    values = environ if environ is not None else os.environ
    environment = values.get("FLOWOPS_ENVIRONMENT", "development").strip().lower()
    if environment == "development":
        return
    short_secrets = [
        name
        for name in ("FLOWOPS_N8N_API_KEY", "FLOWOPS_AUTOMATION_CALLBACK_SECRET")
        if (secret := values.get(name, "")) and len(secret) < 32
    ]
    if short_secrets:
        raise ValueError(
            "Configured workforce integration secrets must contain at least 32 "
            "characters outside development: " + ", ".join(short_secrets)
        )


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
        # Bound expired identities and fail closed on excessive active keys.
        if len(_RATE_LIMIT_BUCKETS) >= 10000:
            expired = [key for key, (start, _) in _RATE_LIMIT_BUCKETS.items() if now - start >= 60]
            for key in expired:
                del _RATE_LIMIT_BUCKETS[key]
            if actor_key not in _RATE_LIMIT_BUCKETS and len(_RATE_LIMIT_BUCKETS) >= 10000:
                raise HTTPException(429, "Rate limit capacity reached; retry later.", headers={"Retry-After": "60"})
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


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]
