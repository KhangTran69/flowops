"""Initial FlowOps schema (baseline for fresh and existing databases).

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-10-17

The upgrade is intentionally metadata-driven: it creates missing tables from
the SQLAlchemy models and adds the additive compatibility columns that older
databases may lack. This makes the baseline safe for both a fresh CI database
and a local database created by ``create_all`` before Alembic existed.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from src import db

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ensure_column(table_name: str, column_name: str, column: sa.Column) -> None:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return
    columns = {entry["name"] for entry in inspector.get_columns(table_name)}
    if column_name not in columns:
        op.add_column(table_name, column)


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    # Keep the historical baseline stable as new model classes are added.
    # These tables are owned by the dedicated 0011_workforce revision.
    deferred_tables = {
        "employees",
        "staff_onboarding_requests",
        "workforce_approval_decisions",
        "it_setup_tasks",
        "it_assets",
        "it_asset_assignments",
        "workforce_audit_events",
        "workforce_automation_events",
        "workforce_reminder_events",
    }
    for table in db.Base.metadata.sorted_tables:
        if table.name not in existing and table.name not in deferred_tables:
            table.create(bind)

    # Additive columns introduced after the original local schema.
    _ensure_column(
        "idempotency_keys", "owner_token", sa.Column("owner_token", sa.String(64))
    )
    _ensure_column(
        "idempotency_keys", "resource_type", sa.Column("resource_type", sa.String(32))
    )
    _ensure_column(
        "execution_logs", "actor_subject", sa.Column("actor_subject", sa.String(256))
    )


def downgrade() -> None:
    """No-op: this baseline may back an existing database with real data."""
    pass
