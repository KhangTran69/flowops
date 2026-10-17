"""SQL reporting layer: hand-written views, a KPI query, and helpers.

The views are plain SQL that runs unchanged on SQLite and PostgreSQL. They
are created by Alembic migration ``0009`` (PostgreSQL also gets the
``flowops_tenant_kpis`` set-returning function) and, for local SQLite
databases that skip Alembic, by :func:`ensure_views`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

# Manual minutes each automated step replaces (used for "time saved" KPIs).
MINUTES_SAVED_CASE = """
    CASE c.step_key
        WHEN 'create_user_account' THEN 15
        WHEN 'grant_software_licenses' THEN 10
        WHEN 'configure_email_mailbox' THEN 10
        WHEN 'disable_user_account' THEN 10
        WHEN 'revoke_software_licenses' THEN 10
        WHEN 'archive_email_mailbox' THEN 20
        ELSE 5
    END"""

VIEWS: dict[str, str] = {
    # Daily success/failure and latency per automation action.
    "v_automation_daily": """
        SELECT
            date(l.created_at) AS day,
            l.action AS action,
            COUNT(*) AS runs,
            SUM(CASE WHEN l.status = 'success' THEN 1 ELSE 0 END) AS succeeded,
            SUM(CASE WHEN l.status <> 'success' THEN 1 ELSE 0 END) AS failed,
            ROUND(AVG(l.duration_ms), 1) AS avg_duration_ms,
            MAX(l.duration_ms) AS max_duration_ms
        FROM execution_logs AS l
        GROUP BY date(l.created_at), l.action
    """,
    # Reliability per adapter/action: first-try success vs. retries vs. failures.
    "v_action_reliability": """
        SELECT
            a.adapter AS adapter,
            a.action AS action,
            COUNT(*) AS attempts,
            SUM(CASE WHEN a.status = 'success' THEN 1 ELSE 0 END) AS succeeded,
            SUM(CASE WHEN a.status = 'success' AND a.attempt = 1 THEN 1 ELSE 0 END)
                AS first_try_successes,
            SUM(CASE WHEN a.attempt > 1 THEN 1 ELSE 0 END) AS retries,
            SUM(CASE WHEN a.status <> 'success' THEN 1 ELSE 0 END) AS failures,
            ROUND(AVG(a.duration_ms), 1) AS avg_duration_ms
        FROM action_executions AS a
        GROUP BY a.adapter, a.action
    """,
    # Open-ticket backlog by tenant, queue and priority.
    "v_ticket_backlog": """
        SELECT
            COALESCE(t.tenant_key, 'default') AS tenant_key,
            COALESCE(t.queue, 'unassigned') AS queue,
            t.priority AS priority,
            COUNT(*) AS open_tickets,
            MIN(t.created_at) AS oldest_created_at
        FROM tickets AS t
        WHERE t.closed_at IS NULL
        GROUP BY COALESCE(t.tenant_key, 'default'), COALESCE(t.queue, 'unassigned'), t.priority
    """,
    # Business KPI: manual minutes replaced by completed automated steps.
    "v_time_saved": f"""
        SELECT
            COALESCE(r.tenant_key, 'default') AS tenant_key,
            r.request_type AS request_type,
            COUNT(*) AS automated_steps,
            SUM({MINUTES_SAVED_CASE}) AS minutes_saved
        FROM checklist_items AS c
        JOIN onboarding_requests AS r ON r.id = c.request_id
        WHERE c.status = 'completed'
          AND c.action IS NOT NULL
          AND c.action <> 'none'
        GROUP BY COALESCE(r.tenant_key, 'default'), r.request_type
    """,
}

# Per-tenant KPI roll-up. Portable SQL; PostgreSQL also exposes it as the
# function flowops_tenant_kpis(p_since timestamptz) (see migration 0009).
TENANT_KPIS_SQL = f"""
    WITH tenant_keys AS (
        SELECT COALESCE(tenant_key, 'default') AS tenant_key FROM onboarding_requests
        UNION
        SELECT COALESCE(tenant_key, 'default') FROM tickets
    ),
    requests AS (
        SELECT
            COALESCE(tenant_key, 'default') AS tenant_key,
            COUNT(*) AS onboarding_requests,
            SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed_requests
        FROM onboarding_requests
        WHERE created_at >= :since
        GROUP BY COALESCE(tenant_key, 'default')
    ),
    backlog AS (
        SELECT COALESCE(tenant_key, 'default') AS tenant_key, COUNT(*) AS open_tickets
        FROM tickets
        WHERE closed_at IS NULL
        GROUP BY COALESCE(tenant_key, 'default')
    ),
    saved AS (
        SELECT
            COALESCE(r.tenant_key, 'default') AS tenant_key,
            SUM({MINUTES_SAVED_CASE}) AS minutes_saved
        FROM checklist_items AS c
        JOIN onboarding_requests AS r ON r.id = c.request_id
        WHERE c.status = 'completed'
          AND c.action IS NOT NULL AND c.action <> 'none'
          AND r.created_at >= :since
        GROUP BY COALESCE(r.tenant_key, 'default')
    )
    SELECT
        k.tenant_key,
        COALESCE(q.onboarding_requests, 0) AS onboarding_requests,
        COALESCE(q.completed_requests, 0) AS completed_requests,
        COALESCE(b.open_tickets, 0) AS open_tickets,
        COALESCE(s.minutes_saved, 0) AS minutes_saved
    FROM tenant_keys AS k
    LEFT JOIN requests AS q ON q.tenant_key = k.tenant_key
    LEFT JOIN backlog AS b ON b.tenant_key = k.tenant_key
    LEFT JOIN saved AS s ON s.tenant_key = k.tenant_key
    ORDER BY minutes_saved DESC, k.tenant_key
"""

PG_TENANT_KPIS_FUNCTION = (
    "CREATE OR REPLACE FUNCTION flowops_tenant_kpis(p_since timestamptz)\n"
    "RETURNS TABLE (tenant_key text, onboarding_requests bigint, completed_requests bigint,\n"
    "               open_tickets bigint, minutes_saved bigint)\n"
    "LANGUAGE sql STABLE AS $$\n"
    + TENANT_KPIS_SQL.replace(":since", "p_since")
    + "\n$$"
)

REQUIRED_TABLES = {"execution_logs", "action_executions", "tickets", "checklist_items", "onboarding_requests"}


def create_view_statement(name: str, dialect: str) -> str:
    verb = "CREATE OR REPLACE VIEW" if dialect == "postgresql" else "CREATE VIEW IF NOT EXISTS"
    return f"{verb} {name} AS {VIEWS[name]}"


def create_views(connection: Connection) -> None:
    dialect = connection.dialect.name
    for name in VIEWS:
        connection.execute(text(create_view_statement(name, dialect)))
    if dialect == "postgresql":
        connection.execute(text(PG_TENANT_KPIS_FUNCTION))


def drop_views(connection: Connection) -> None:
    for name in reversed(list(VIEWS)):
        connection.execute(text(f"DROP VIEW IF EXISTS {name}"))
    if connection.dialect.name == "postgresql":
        connection.execute(text("DROP FUNCTION IF EXISTS flowops_tenant_kpis(timestamptz)"))


def ensure_views(engine: Engine) -> None:
    """Create reporting views if the base tables exist (idempotent)."""
    if not REQUIRED_TABLES.issubset(set(inspect(engine).get_table_names())):
        return
    with engine.begin() as connection:
        create_views(connection)


def _rows(connection: Connection, sql: str, **params: Any) -> list[dict[str, Any]]:
    return [dict(row._mapping) for row in connection.execute(text(sql), params)]


def automation_daily(connection: Connection, days: int = 14) -> list[dict[str, Any]]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    return _rows(
        connection,
        "SELECT * FROM v_automation_daily WHERE day >= :since ORDER BY day DESC, action",
        since=since,
    )


def action_reliability(connection: Connection) -> list[dict[str, Any]]:
    return _rows(connection, "SELECT * FROM v_action_reliability ORDER BY failures DESC, attempts DESC")


def ticket_backlog(connection: Connection) -> list[dict[str, Any]]:
    return _rows(
        connection,
        "SELECT * FROM v_ticket_backlog ORDER BY open_tickets DESC, tenant_key, queue",
    )


def time_saved(connection: Connection) -> list[dict[str, Any]]:
    return _rows(connection, "SELECT * FROM v_time_saved ORDER BY minutes_saved DESC")


def tenant_kpis(connection: Connection, days: int = 30) -> list[dict[str, Any]]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    if connection.dialect.name == "postgresql":
        return _rows(connection, "SELECT * FROM flowops_tenant_kpis(:since)", since=since)
    return _rows(connection, TENANT_KPIS_SQL, since=since.isoformat(sep=" "))


def summary(connection: Connection, days: int = 30) -> dict[str, Any]:
    return {
        "window_days": days,
        "automation_daily": automation_daily(connection, days=min(days, 31)),
        "action_reliability": action_reliability(connection),
        "ticket_backlog": ticket_backlog(connection),
        "time_saved": time_saved(connection),
        "tenant_kpis": tenant_kpis(connection, days=days),
    }
