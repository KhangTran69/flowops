"""Signed operator-console sessions and CSRF helpers.

The API keeps bearer auth; the browser console uses a small signed cookie. We
use ``itsdangerous`` directly rather than adding a session framework so the
cookie flags, expiry, and CSRF behavior stay explicit and testable offline.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError

from src import db
from src.auth import Actor, AuthenticationError, _identity_subject, _identity_tenant

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
    tenant_key: str | None = None


def _values(environ: Mapping[str, str] | None = None) -> Mapping[str, str]:
    return environ if environ is not None else os.environ


def ui_auth_required(environ: Mapping[str, str] | None = None) -> bool:
    return _values(environ).get("FLOWOPS_UI_AUTH_REQUIRED", "false").lower() == "true"


def session_secret(environ: Mapping[str, str] | None = None) -> str:
    secret = _values(environ).get("FLOWOPS_SESSION_SECRET", "")
    if not secret:
        raise UISessionError(
            "FLOWOPS_SESSION_SECRET is required when UI auth is enabled."
        )
    return secret


def session_max_age(environ: Mapping[str, str] | None = None) -> int:
    try:
        configured = int(
            _values(environ).get(
                "FLOWOPS_SESSION_MAX_AGE_SECONDS", str(DEFAULT_MAX_AGE_SECONDS)
            )
        )
    except (TypeError, ValueError):
        configured = DEFAULT_MAX_AGE_SECONDS
    return max(60, min(configured, 86_400))


def cookie_secure(environ: Mapping[str, str] | None = None) -> bool:
    return _values(environ).get("FLOWOPS_SESSION_COOKIE_SECURE", "false").lower() == "true"


def _serializer(secret: str | None = None) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        secret or session_secret(), salt=SESSION_SALT
    )


def validate_ui_auth_configuration(
    environ: Mapping[str, str] | None = None,
) -> None:
    """Fail fast when protected UI mode lacks a secret or auth source."""
    values = _values(environ)
    if not ui_auth_required(values):
        if values.get("FLOWOPS_ENVIRONMENT", "development").lower() != "development":
            raise UISessionError(
                "FLOWOPS_UI_AUTH_REQUIRED=true is mandatory outside development."
            )
        return
    session_secret_value = values.get("FLOWOPS_SESSION_SECRET", "")
    if len(session_secret_value) < 32:
        raise UISessionError(
            "FLOWOPS_UI_AUTH_REQUIRED=true requires FLOWOPS_SESSION_SECRET with at least 32 characters."
        )
    if values.get("FLOWOPS_ENVIRONMENT", "development").lower() != "development" and not cookie_secure(values):
        raise UISessionError("FLOWOPS_SESSION_COOKIE_SECURE=true is mandatory outside development.")
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


def hash_demo_password(password: str, *, salt: str | None = None) -> str:
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


def authenticate_admin(username: str, password: str) -> Actor | None:
    """Authenticate only explicitly configured, development-gated accounts."""
    if os.environ.get("FLOWOPS_ENVIRONMENT", "development").lower() != "development":
        return None
    if not username or len(username) > 256 or not password or len(password) > 1024:
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
    values = account.get("roles")
    if not isinstance(values, list) or not values or any(
        not isinstance(role, str) or role not in _WORKFORCE_ROLES for role in values
    ):
        return None
    try:
        subject = _identity_subject(username)
        tenant_key = _identity_tenant(account.get("tenant_key"))
    except AuthenticationError:
        return None
    return Actor(subject=subject, roles=tuple(values), source="session", tenant_key=tenant_key)


def create_session(actor: Actor) -> UISession:
    return UISession(
        subject=actor.subject,
        roles=tuple(actor.roles),
        csrf_token=secrets.token_urlsafe(32),
        tenant_key=actor.tenant_key,
    )


def _account_configuration_revision(secret: str | None = None) -> str:
    """Bind development sessions to the configured account set, not a raw hash.

    Account removal, role/tenant edits and password rotation invalidate
    existing browser sessions across processes without retaining credentials
    in the cookie. No production browser issuer is enabled.
    """
    raw = os.environ.get("FLOWOPS_DEMO_USERS", "{}")
    return hmac.new((secret or session_secret()).encode(),
                    b"flowops-demo-account-revision:" + raw.encode(), hashlib.sha256).hexdigest()


def encode_session(session: UISession, secret: str | None = None) -> str:
    return _serializer(secret).dumps(
        {
            "subject": session.subject,
            "roles": list(session.roles),
            "csrf": session.csrf_token,
            "tenant_key": session.tenant_key,
            "account_revision": _account_configuration_revision(secret),
        }
    )


def decode_session(
    value: str,
    *,
    max_age: int | None = None,
    secret: str | None = None,
) -> UISession:
    try:
        data = _serializer(secret).loads(
            value, max_age=max_age if max_age is not None else session_max_age()
        )
    except (BadSignature, SignatureExpired, TypeError, ValueError) as error:
        raise UISessionError("Invalid or expired UI session.") from error
    if not isinstance(data, dict):
        raise UISessionError("Invalid UI session payload.")
    roles = data.get("roles")
    csrf = data.get("csrf")
    revision = data.get("account_revision")
    if not isinstance(revision, str) or not secrets.compare_digest(
        revision.encode(), _account_configuration_revision(secret).encode()
    ):
        raise UISessionError("UI session account configuration changed or is invalid.")
    if (
        not isinstance(roles, list) or not roles
        or any(not isinstance(role, str) or not role or role != role.strip() for role in roles)
        or not isinstance(csrf, str) or not csrf or csrf != csrf.strip()
    ):
        raise UISessionError("Invalid UI session payload.")
    try:
        subject = _identity_subject(data.get("subject"))
        tenant_key = _identity_tenant(data.get("tenant_key"))
    except AuthenticationError as error:
        raise UISessionError("Invalid UI session identity.") from error
    return UISession(subject=subject, roles=tuple(roles), csrf_token=csrf, tenant_key=tenant_key)


def _revocation_digest(session: UISession) -> str:
    return hashlib.sha256(session.csrf_token.encode()).hexdigest()


def revoke_session(session_db, session: UISession) -> None:
    # Retain for the maximum supported cookie age, including configuration changes.
    now = db.utcnow()
    session_db.execute(delete(db.RevokedUISessionRecord).where(
        db.RevokedUISessionRecord.expires_at <= now
    ))
    digest = _revocation_digest(session)
    table = db.RevokedUISessionRecord.__table__
    if session_db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    session_db.execute(insert(table).values(
        digest=digest, expires_at=now + timedelta(days=1)
    ).on_conflict_do_nothing(index_elements=["digest"]))


def read_session(request) -> UISession | None:
    # The only cookie issuer currently implemented is the development login.
    # A signed development cookie is not evidence of provider authentication.
    if os.environ.get("FLOWOPS_ENVIRONMENT", "development").lower() != "development":
        return None
    value = request.cookies.get(SESSION_COOKIE)
    if not value or len(value) > 8192:
        return None
    try:
        session = decode_session(value)
        db._ensure_tables()
        with db.SessionLocal() as session_db:
            if session_db.get(db.RevokedUISessionRecord, _revocation_digest(session)) is not None:
                return None
        return session
    except (UISessionError, SQLAlchemyError):
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
    response.delete_cookie(SESSION_COOKIE, path="/", samesite="lax", httponly=True, secure=cookie_secure())


def actor_from_session(session: UISession) -> Actor:
    return Actor(subject=session.subject, roles=session.roles, source="session", tenant_key=session.tenant_key)


def csrf_matches(request, session: UISession) -> bool:
    supplied = request.headers.get("X-CSRF-Token")
    if not supplied:
        return False
    return secrets.compare_digest(supplied.encode(), session.csrf_token.encode())


def safe_next(value: str | None, default: str = "/dashboard") -> str:
    """Only allow same-site absolute paths; reject schemes/protocol-relative."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return default
    if "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return default
    return value
