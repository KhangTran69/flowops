"""Fail closed before a protected Fly release changes the database schema."""
import os
import subprocess
from urllib.parse import urlsplit

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from src import ui_session
from src.auth import validate_auth_configuration
from src.deps import validate_webhook_configuration, validate_workforce_integration_configuration


def validate_release(environ: dict[str, str]) -> None:
    """Validate configuration without contacting the provider or database."""
    if environ.get("FLOWOPS_ENVIRONMENT") != "production":
        raise ValueError("Protected Fly releases require production mode.")
    if environ.get("FLOWOPS_RUN_MIGRATIONS") != "false":
        raise ValueError("Only the release command may run Fly migrations.")
    if environ.get("FLOWOPS_DEV_BEARER_TOKEN") or environ.get("FLOWOPS_DEMO_USERS"):
        raise ValueError("Development credentials must not be configured for protected releases.")
    for name in ("FLOWOPS_ACTION_ADAPTER", "FLOWOPS_AUTOMATION_ADAPTER", "FLOWOPS_AI_ADAPTER"):
        if environ.get(name) != "simulated":
            raise ValueError(f"{name} must remain simulated for this release candidate.")
    for name in ("FLOWOPS_NOTIFICATION_WEBHOOK_URL", "FLOWOPS_ACTION_FAILURE_WEBHOOK_URL",
                 "FLOWOPS_N8N_WEBHOOK_URL", "FLOWOPS_N8N_API_KEY",
                 "FLOWOPS_AUTOMATION_CALLBACK_SECRET", "FLOWOPS_N8N_WEBHOOK_SECRET"):
        if environ.get(name):
            raise ValueError(f"{name} must remain unset for the isolated simulated candidate.")
    if environ.get("FLOWOPS_SESSION_SECRET") == environ.get("FLOWOPS_WEBHOOK_API_KEY"):
        raise ValueError("Session and webhook credentials must be distinct.")
    for name in ("FLOWOPS_ENTRA_ISSUER", "FLOWOPS_ENTRA_JWKS_URL"):
        try:
            parsed = urlsplit(environ.get(name, ""))
            valid = (parsed.scheme == "https" and bool(parsed.hostname)
                     and parsed.username is None and parsed.password is None
                     and not parsed.query and not parsed.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(f"{name} must be a trusted HTTPS URL.")
    try:
        url = make_url(environ.get("FLOWOPS_DATABASE_URL", ""))
    except (ArgumentError, ValueError):
        raise ValueError("A valid PostgreSQL database URL is required.") from None
    if url.drivername != "postgresql+psycopg" or not url.host or not url.database:
        raise ValueError("A PostgreSQL psycopg database URL with host and database is required.")
    try:
        limit = int(environ.get("FLOWOPS_RATE_LIMIT_PER_MINUTE", "0"))
    except ValueError:
        raise ValueError("Protected releases require a valid positive rate limit.") from None
    if not 1 <= limit <= 100000:
        raise ValueError("Protected releases require a positive rate limit no greater than 100000.")
    validate_auth_configuration(environ)
    ui_session.validate_ui_auth_configuration(environ)
    validate_webhook_configuration(environ)
    validate_workforce_integration_configuration(environ)


# Stable application-specific session lock serializes release migrations.
RELEASE_LOCK_ID = 7061408101


def migrate_once(database_url: str) -> None:
    engine = create_engine(database_url, connect_args={"connect_timeout": 10})
    try:
        with engine.connect() as connection:
            acquired = connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": RELEASE_LOCK_ID}
            ).scalar_one()
            if not acquired:
                raise RuntimeError("Another FlowOps release owns the database migration lock.")
            try:
                subprocess.run(["alembic", "upgrade", "head"], check=True, timeout=600)
            finally:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": RELEASE_LOCK_ID})
    finally:
        engine.dispose()


def main() -> None:
    environ = dict(os.environ)
    validate_release(environ)
    migrate_once(environ["FLOWOPS_DATABASE_URL"])


if __name__ == "__main__":
    main()
