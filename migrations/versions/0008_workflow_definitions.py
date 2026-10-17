"""Add versioned workflow definitions, joins/predicates, and SLA timers.

Revision ID: 0008_workflow_definitions
Revises: 0007_workflow_orchestrator
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_workflow_definitions"
down_revision: Union[str, None] = "0007_workflow_orchestrator"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "workflow_definitions" not in tables:
        op.create_table(
            "workflow_definitions",
            sa.Column("key", sa.String(80), primary_key=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("request_type", sa.String(16), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
            sa.Column("current_version", sa.Integer()),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )
    if "workflow_definition_versions" not in tables:
        op.create_table(
            "workflow_definition_versions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("definition_key", sa.String(80), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("steps_json", sa.Text(), nullable=False),
            sa.Column("checksum", sa.String(64), nullable=False),
            sa.Column("created_by", sa.String(120)),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("published_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint(
                "definition_key", "version", name="uq_workflow_definition_version"
            ),
        )
        op.create_index(
            "ix_workflow_definition_versions_key",
            "workflow_definition_versions",
            ["definition_key"],
        )
    if "workflow_timers" not in tables:
        op.create_table(
            "workflow_timers",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("request_id", sa.Integer(), nullable=False),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False, server_default="step_sla"),
            sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("lease_owner_token", sa.String(64)),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
            sa.Column("fired_at", sa.DateTime(timezone=True)),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        )
        op.create_index(
            "ix_workflow_timers_status_due", "workflow_timers", ["status", "due_at"]
        )
    for column, spec in (
        ("definition_key", sa.String(80)),
        ("definition_version", sa.Integer()),
    ):
        columns = {c["name"] for c in inspector.get_columns("onboarding_requests")}
        if column not in columns:
            op.add_column("onboarding_requests", sa.Column(column, spec))
    columns = {c["name"] for c in inspector.get_columns("checklist_items")}
    for column, spec in (
        ("join_mode", sa.String(8)),
        ("when_json", sa.Text()),
        ("sla_minutes", sa.Integer()),
        ("escalation_queue", sa.String(64)),
    ):
        if column not in columns:
            op.add_column("checklist_items", sa.Column(column, spec))


def downgrade() -> None:
    """No-op: keep definition and timer history."""
    pass
