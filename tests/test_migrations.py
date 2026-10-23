"""Tests for the Alembic baseline migration."""

from alembic import command
from alembic.config import Config
from datetime import datetime, timezone

from sqlalchemy import create_engine, inspect, text


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
        "employees",
        "staff_onboarding_requests",
        "workforce_approval_decisions",
        "it_setup_tasks",
        "it_assets",
        "it_asset_assignments",
        "workforce_audit_events",
        "workforce_automation_events",
        "workforce_reminder_events",
        "tenants",
        "alembic_version",
    } <= tables


def test_workforce_tenant_migration_preserves_legacy_rows_as_unassigned(tmp_path):
    url = f"sqlite:///{tmp_path}/legacy-workforce.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0011_workforce")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO employees (employee_number, full_name, work_email, department, job_title, start_date, manager_subject, status, created_at) VALUES (:number, :name, :email, :department, :title, :start_date, :manager, :status, :created_at)"), {
                "number": "LEGACY-1", "name": "Legacy Person", "email": "legacy@example.test",
                "department": "IT", "title": "Analyst", "start_date": "2027-01-01",
                "manager": "manager-old", "status": "ACTIVE",
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
        command.upgrade(config, "head")
        with engine.connect() as connection:
            row = connection.execute(text("SELECT employee_number, tenant_key FROM employees WHERE employee_number = 'LEGACY-1'")).one()
            columns = {column["name"] for column in inspect(connection).get_columns("workforce_automation_events")}
        assert row.employee_number == "LEGACY-1"
        assert row.tenant_key is None
        assert "lease_token" in columns
    finally:
        engine.dispose()
