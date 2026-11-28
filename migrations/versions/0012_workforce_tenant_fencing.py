"""Scope workforce data to tenants and fence expired outbox leases.

Revision ID: 0012_workforce_tenant_fencing
Revises: 0011_workforce
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_workforce_tenant_fencing"
down_revision: Union[str, None] = "0011_workforce"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TENANT_TABLES = (
    "employees",
    "staff_onboarding_requests",
    "workforce_approval_decisions",
    "it_setup_tasks",
    "it_assets",
    "it_asset_assignments",
    "workforce_audit_events",
    "workforce_automation_events",
    "workforce_reminder_events",
)


def _duplicate_details(bind) -> list[str]:
    """Return actionable descriptions for rows that conflict with new unique indexes."""
    inspector = sa.inspect(bind)
    request_groups = bind.execute(sa.text("""
        SELECT employee_id, COUNT(*) AS row_count
        FROM staff_onboarding_requests
        WHERE status IN ('DRAFT','PENDING_APPROVAL','APPROVED','IN_PROGRESS')
        GROUP BY employee_id HAVING COUNT(*) > 1
    """)).all()
    details = []
    for group in request_groups:
        ids = bind.execute(sa.text("""
            SELECT id FROM staff_onboarding_requests
            WHERE employee_id = :employee_id
              AND status IN ('DRAFT','PENDING_APPROVAL','APPROVED','IN_PROGRESS')
            ORDER BY id
        """), {"employee_id": group.employee_id}).scalars().all()
        details.append(f"uq_staff_onboarding_one_active employee_id={group.employee_id} request_ids={ids}")

    audit_columns = {column["name"] for column in inspector.get_columns("workforce_audit_events")}
    tenant_column = "tenant_key" if "tenant_key" in audit_columns else "NULL"
    tenant_group = "tenant_key," if tenant_column == "tenant_key" else ""
    groups = bind.execute(sa.text(f"""
        SELECT {tenant_group} entity_id, COUNT(*) AS row_count
        FROM workforce_audit_events
        WHERE action = 'automation.error' AND entity_type = 'automation_event'
        GROUP BY {tenant_group} entity_id HAVING COUNT(*) > 1
    """)).all()
    for group in groups:
        tenant_key = getattr(group, "tenant_key", None)
        if tenant_column == "tenant_key" and tenant_key is None:
            tenant_predicate, params = "tenant_key IS NULL", {"entity_id": group.entity_id}
        elif tenant_column == "tenant_key":
            tenant_predicate, params = "tenant_key = :tenant_key", {
                "entity_id": group.entity_id, "tenant_key": tenant_key,
            }
        else:
            tenant_predicate, params = "1 = 1", {"entity_id": group.entity_id}
        ids = bind.execute(sa.text(f"""
            SELECT id FROM workforce_audit_events
            WHERE action = 'automation.error' AND entity_type = 'automation_event'
              AND entity_id = :entity_id AND {tenant_predicate}
            ORDER BY id
        """), params).scalars().all()
        index_name = "uq_workforce_error_tenant" if tenant_key is not None else "uq_workforce_error_global"
        details.append(f"{index_name} entity_id={group.entity_id} audit_ids={ids}")
    return details


def upgrade() -> None:
    bind = op.get_bind()
    conflicts = _duplicate_details(bind)
    if conflicts:
        raise RuntimeError(
            "Cannot apply workforce tenant fencing migration: resolve duplicate records first, "
            "then retry. Conflicts: " + "; ".join(conflicts)
        )
    inspector = sa.inspect(bind)
    for table in TENANT_TABLES:
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "tenant_key" not in columns:
            op.add_column(table, sa.Column("tenant_key", sa.String(64), nullable=True))
            op.create_index(f"ix_{table}_tenant_key", table, ["tenant_key"])
    event_columns = {column["name"] for column in inspector.get_columns("workforce_automation_events")}
    if "lease_token" not in event_columns:
        op.add_column("workforce_automation_events", sa.Column("lease_token", sa.String(64), nullable=True))
    op.create_index(
        "ix_staff_onboarding_tenant_status", "staff_onboarding_requests", ["tenant_key", "status"]
    )
    op.create_index(
        "uq_staff_onboarding_one_active", "staff_onboarding_requests", ["employee_id"], unique=True,
        sqlite_where=sa.text("status IN ('DRAFT','PENDING_APPROVAL','APPROVED','IN_PROGRESS')"),
        postgresql_where=sa.text("status IN ('DRAFT','PENDING_APPROVAL','APPROVED','IN_PROGRESS')"),
    )
    op.create_index("ix_employees_tenant_status", "employees", ["tenant_key", "status"])
    op.create_index(
        "uq_workforce_error_tenant", "workforce_audit_events", ["tenant_key", "entity_id"], unique=True,
        sqlite_where=sa.text("action = 'automation.error' AND entity_type = 'automation_event' AND tenant_key IS NOT NULL"),
        postgresql_where=sa.text("action = 'automation.error' AND entity_type = 'automation_event' AND tenant_key IS NOT NULL"),
    )
    op.create_index(
        "uq_workforce_error_global", "workforce_audit_events", ["entity_id"], unique=True,
        sqlite_where=sa.text("action = 'automation.error' AND entity_type = 'automation_event' AND tenant_key IS NULL"),
        postgresql_where=sa.text("action = 'automation.error' AND entity_type = 'automation_event' AND tenant_key IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_workforce_error_global", table_name="workforce_audit_events")
    op.drop_index("uq_workforce_error_tenant", table_name="workforce_audit_events")
    op.drop_index("ix_employees_tenant_status", table_name="employees")
    op.drop_index("uq_staff_onboarding_one_active", table_name="staff_onboarding_requests")
    op.drop_index("ix_staff_onboarding_tenant_status", table_name="staff_onboarding_requests")
    op.drop_column("workforce_automation_events", "lease_token")
    for table in reversed(TENANT_TABLES):
        op.drop_index(f"ix_{table}_tenant_key", table_name=table)
        op.drop_column(table, "tenant_key")
