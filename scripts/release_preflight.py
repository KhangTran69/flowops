"""Fail closed before a protected Fly release changes the database schema."""
import os
import subprocess

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
    if environ.get("FLOWOPS_NOTIFICATION_WEBHOOK_URL"):
        raise ValueError("Outbound notification webhooks must remain disabled.")
    try:
        url = make_url(environ.get("FLOWOPS_DATABASE_URL", ""))
    except (ArgumentError, ValueError):
        raise ValueError("A valid PostgreSQL database URL is required.") from None
    if url.drivername != "postgresql+psycopg" or not url.host or not url.database:
        raise ValueError("A PostgreSQL psycopg database URL with host and database is required.")
    validate_auth_configuration(environ)
    ui_session.validate_ui_auth_configuration(environ)
    validate_webhook_configuration(environ)
    validate_workforce_integration_configuration(environ)


def main() -> None:
    validate_release(dict(os.environ))
    subprocess.run(["alembic", "upgrade", "head"], check=True)


if __name__ == "__main__":
    main()
