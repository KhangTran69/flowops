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


def upgrade() -> None:
    bind = op.get_bind()
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
