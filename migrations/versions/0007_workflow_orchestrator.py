"""Add workflow run, step-attempt, and event tables.

Revision ID: 0007_workflow_orchestrator
Revises: 0006_workflow_dag_chatops
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_workflow_orchestrator"
down_revision: Union[str, None] = "0006_workflow_dag_chatops"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "workflow_runs" not in tables:
        op.create_table(
            "workflow_runs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("request_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("max_parallel", sa.Integer(), nullable=False, server_default="4"),
            sa.Column("lease_owner_token", sa.String(64)),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
            sa.Column("last_tick_at", sa.DateTime(timezone=True)),
            sa.Column("error", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("started_at", sa.DateTime(timezone=True)),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("request_id", name="uq_workflow_run_request"),
        )
        op.create_index(
            "ix_workflow_runs_status_lease",
            "workflow_runs",
            ["status", "lease_expires_at"],
        )
    if "workflow_step_runs" not in tables:
        op.create_table(
            "workflow_step_runs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("run_id", sa.Integer(), nullable=False),
            sa.Column("checklist_item_id", sa.Integer(), nullable=False),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("job_id", sa.Integer()),
            sa.Column("scheduled_at", sa.DateTime(timezone=True)),
            sa.Column("started_at", sa.DateTime(timezone=True)),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.Column("output_json", sa.Text()),
            sa.Column("error", sa.Text()),
            sa.UniqueConstraint(
                "run_id", "checklist_item_id", "attempt", name="uq_workflow_step_attempt"
            ),
        )
        op.create_index(
            "ix_workflow_step_runs_run_status", "workflow_step_runs", ["run_id", "status"]
        )
    if "workflow_events" not in tables:
        op.create_table(
            "workflow_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("run_id", sa.Integer(), nullable=False),
            sa.Column("step_run_id", sa.Integer()),
            sa.Column("event_type", sa.String(64), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True)),
        )
        op.create_index(
            "ix_workflow_events_run_created", "workflow_events", ["run_id", "created_at"]
        )


def downgrade() -> None:
    """No-op: keep orchestration history."""
    pass
