"""Shared FastAPI dependencies: actors, roles, CSRF, rate limiting."""

import os
import secrets
import threading
import time
import uuid
from dataclasses import replace
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request

from src import db, ui_session
from src.auth import (
    AuthenticationError,
    Actor,
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


def _actor_tenant(actor: Actor) -> Optional[str]:
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
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
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
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    x_tenant_key: Optional[str] = Header(default=None, alias="X-Tenant-Key"),
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


def validate_webhook_configuration(environ: Optional[dict[str, str]] = None) -> None:
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
    environ: Optional[dict[str, str]] = None,
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


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]

