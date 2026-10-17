"""Add event-driven trigger layer: event sources, triggers, deliveries.

Revision ID: 0009_event_triggers
Revises: 0008_workflow_definitions
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_event_triggers"
down_revision: Union[str, None] = "0008_workflow_definitions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "event_sources" not in tables:
        op.create_table(
            "event_sources",
            sa.Column("key", sa.String(80), primary_key=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("secret", sa.String(200), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(timezone=True)),
        )

    if "triggers" not in tables:
        op.create_table(
            "triggers",
            sa.Column("key", sa.String(80), primary_key=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("source_key", sa.String(80), nullable=False),
            sa.Column("event_type", sa.String(120), nullable=False),
            sa.Column("target_type", sa.String(20), nullable=False),
            sa.Column("definition_key", sa.String(80)),
            sa.Column("mapping_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column(
                "schedule_kind",
                sa.String(10),
                nullable=False,
                server_default="webhook",
            ),
            sa.Column("interval_seconds", sa.Integer()),
            sa.Column("daily_at_utc", sa.String(8)),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("last_fired_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_triggers_source_key", "triggers", ["source_key"])

    if "trigger_deliveries" not in tables:
        op.create_table(
            "trigger_deliveries",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("trigger_key", sa.String(80), nullable=False),
            sa.Column("event_id", sa.String(200), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column(
                "status", sa.String(16), nullable=False, server_default="received"
            ),
            sa.Column("request_id", sa.String(64)),
            sa.Column("error", sa.Text()),
            sa.Column("received_at", sa.DateTime(timezone=True)),
            sa.Column("processed_at", sa.DateTime(timezone=True)),
            sa.Column("lease_owner_token", sa.String(64)),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("available_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint(
                "trigger_key", "event_id", name="uq_trigger_delivery_event"
            ),
        )
        op.create_index(
            "ix_trigger_deliveries_trigger_key",
            "trigger_deliveries",
            ["trigger_key"],
        )
        op.create_index(
            "ix_trigger_deliveries_status_available",
            "trigger_deliveries",
            ["status", "available_at"],
        )


def downgrade() -> None:
    """No-op: keep trigger history."""
    pass
