"""Tests for the Alembic baseline migration."""

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_alembic_upgrade_creates_all_tables_and_is_repeatable(tmp_path):
    url = f"sqlite:///{tmp_path}/migrated.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)

    command.upgrade(config, "head")
    command.upgrade(config, "head")  # idempotent

    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert {
        "tickets",
        "execution_logs",
        "idempotency_keys",
        "action_executions",
        "notification_outbox",
        "onboarding_requests",
        "checklist_items",
        "workflow_jobs",
        "alembic_version",
    } <= tables
