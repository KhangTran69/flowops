"""Signed operator-console sessions and CSRF helpers.

The API keeps bearer auth; the browser console uses a small signed cookie. We
use ``itsdangerous`` directly rather than adding a session framework so the
cookie flags, expiry, and CSRF behavior stay explicit and testable offline.
"""

from __future__ import annotations

import os
import secrets
import hashlib
import json
from dataclasses import dataclass
from typing import Mapping, Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from src.auth import Actor

SESSION_COOKIE = "flowops_ui_session"
SESSION_SALT = "flowops-ui-session"
DEFAULT_MAX_AGE_SECONDS = 3600


class UISessionError(ValueError):
    """Raised when UI-session configuration or a cookie is invalid."""


@dataclass(frozen=True)
class UISession:
    subject: str
    roles: tuple[str, ...]
    csrf_token: str
    tenant_key: Optional[str] = None


def _values(environ: Optional[Mapping[str, str]] = None) -> Mapping[str, str]:
    return environ if environ is not None else os.environ


def ui_auth_required(environ: Optional[Mapping[str, str]] = None) -> bool:
    return _values(environ).get("FLOWOPS_UI_AUTH_REQUIRED", "false").lower() == "true"


def session_secret(environ: Optional[Mapping[str, str]] = None) -> str:
    secret = _values(environ).get("FLOWOPS_SESSION_SECRET", "")
    if not secret:
        raise UISessionError(
            "FLOWOPS_SESSION_SECRET is required when UI auth is enabled."
        )
    return secret


def session_max_age(environ: Optional[Mapping[str, str]] = None) -> int:
    try:
        configured = int(
            _values(environ).get(
                "FLOWOPS_SESSION_MAX_AGE_SECONDS", str(DEFAULT_MAX_AGE_SECONDS)
            )
        )
    except (TypeError, ValueError):
        configured = DEFAULT_MAX_AGE_SECONDS
    return max(60, min(configured, 86_400))


def cookie_secure(environ: Optional[Mapping[str, str]] = None) -> bool:
    return _values(environ).get("FLOWOPS_SESSION_COOKIE_SECURE", "false").lower() == "true"


def _serializer(secret: Optional[str] = None) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        secret or session_secret(), salt=SESSION_SALT
    )


def validate_ui_auth_configuration(
    environ: Optional[Mapping[str, str]] = None,
) -> None:
    """Fail fast when protected UI mode lacks a secret or auth source."""
    values = _values(environ)
    if not ui_auth_required(values):
        return
    if not values.get("FLOWOPS_SESSION_SECRET"):
        raise UISessionError(
            "FLOWOPS_UI_AUTH_REQUIRED=true requires FLOWOPS_SESSION_SECRET."
        )
    if values.get("FLOWOPS_AUTH_REQUIRED", "false").lower() != "true":
        raise UISessionError(
            "FLOWOPS_UI_AUTH_REQUIRED=true requires FLOWOPS_AUTH_REQUIRED=true "
            "so pasted tokens are actually validated."
        )
    has_entra = all(
        values.get(name)
        for name in (
            "FLOWOPS_ENTRA_ISSUER",
            "FLOWOPS_ENTRA_AUDIENCE",
            "FLOWOPS_ENTRA_JWKS_URL",
        )
    )
    has_dev_token = (
        values.get("FLOWOPS_ENVIRONMENT", "development").lower() == "development"
        and bool(values.get("FLOWOPS_DEV_BEARER_TOKEN"))
    )
    if not has_entra and not has_dev_token:
        raise UISessionError(
            "Protected UI mode needs an Entra configuration or a development "
            "FLOWOPS_DEV_BEARER_TOKEN."
        )


_PASSWORD_HASH_ROUNDS = 310_000
_WORKFORCE_ROLES = {"HR", "MANAGER", "IT_ADMIN", "automation.operator", "automation.admin"}


def hash_demo_password(password: str, *, salt: Optional[str] = None) -> str:
    """Create a PBKDF2-SHA256 hash for a development demo account."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PASSWORD_HASH_ROUNDS).hex()
    return f"pbkdf2_sha256${_PASSWORD_HASH_ROUNDS}${salt}${digest}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256" or not 100_000 <= int(rounds) <= 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds)).hex()
        return secrets.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def authenticate_admin(username: str, password: str) -> Optional[Actor]:
    """Authenticate only explicitly configured, development-gated accounts."""
    if os.environ.get("FLOWOPS_ENVIRONMENT", "development").lower() != "development":
        return None
    try:
        users = json.loads(os.environ.get("FLOWOPS_DEMO_USERS", "{}"))
    except json.JSONDecodeError:
        return None
    if not isinstance(users, dict):
        return None
    account = users.get(username)
    if not isinstance(account, dict) or not _verify_password(password, str(account.get("password_hash", ""))):
        return None
    roles = tuple(str(role) for role in account.get("roles", []))
    if not roles or any(role not in _WORKFORCE_ROLES for role in roles):
        return None
    tenant_key = account.get("tenant_key")
    if tenant_key is not None and (not isinstance(tenant_key, str) or not tenant_key.strip()):
        return None
    return Actor(subject=username, roles=roles, source="session", tenant_key=tenant_key)


def create_session(actor: Actor) -> UISession:
    return UISession(
        subject=actor.subject,
        roles=tuple(actor.roles),
        csrf_token=secrets.token_urlsafe(32),
        tenant_key=actor.tenant_key,
    )


def encode_session(session: UISession, secret: Optional[str] = None) -> str:
    return _serializer(secret).dumps(
        {
            "subject": session.subject,
            "roles": list(session.roles),
            "csrf": session.csrf_token,
            "tenant_key": session.tenant_key,
        }
    )


def decode_session(
    value: str,
    *,
    max_age: Optional[int] = None,
    secret: Optional[str] = None,
) -> UISession:
    try:
        data = _serializer(secret).loads(
            value, max_age=max_age if max_age is not None else session_max_age()
        )
    except (BadSignature, SignatureExpired, TypeError, ValueError) as error:
        raise UISessionError("Invalid or expired UI session.") from error
    subject = str(data.get("subject", "")).strip()
    roles = tuple(str(role) for role in data.get("roles", []))
    csrf = str(data.get("csrf", ""))
    tenant_key = data.get("tenant_key")
    if tenant_key is not None:
        tenant_key = str(tenant_key).strip()
        if not tenant_key:
            raise UISessionError("Invalid UI session tenant.")
    if not subject or not csrf or not roles:
        raise UISessionError("Invalid UI session payload.")
    return UISession(subject=subject, roles=roles, csrf_token=csrf, tenant_key=tenant_key)


def read_session(request) -> Optional[UISession]:
    value = request.cookies.get(SESSION_COOKIE)
    if not value:
        return None
    try:
        return decode_session(value)
    except UISessionError:
        return None


def set_session_cookie(response, session: UISession) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        encode_session(session),
        max_age=session_max_age(),
        httponly=True,
        samesite="lax",
        secure=cookie_secure(),
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", samesite="lax")


def actor_from_session(session: UISession) -> Actor:
    return Actor(subject=session.subject, roles=session.roles, source="session", tenant_key=session.tenant_key)


def csrf_matches(request, session: UISession) -> bool:
    supplied = request.headers.get("X-CSRF-Token")
    if not supplied:
        return False
    return secrets.compare_digest(supplied, session.csrf_token)


def safe_next(value: Optional[str], default: str = "/dashboard") -> str:
    """Only allow same-site absolute paths; reject schemes/protocol-relative."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return default
    if "\\" in value or "\n" in value or "\r" in value:
        return default
    return value
