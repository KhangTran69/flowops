"""Shared FastAPI dependencies: actors, roles, CSRF, rate limiting."""

import os
import secrets
import threading
import time
import uuid
from dataclasses import replace
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request

from src import ui_session
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
    if expected and not secrets.compare_digest(x_api_key or "", expected):
        raise HTTPException(status_code=401, detail="Invalid webhook API key.")
    _enforce_rate_limit("webhook:intake")
    return "webhook-api-key" if expected else "local-development"


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

