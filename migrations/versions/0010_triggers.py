"""Add event-driven triggers and delivery tracking (v1.4).

Revision ID: 0010_triggers
Revises: 0009_reporting_view
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_triggers"
down_revision: Union[str, None] = "0009_tenants_reporting"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    # event_sources table
    if "event_sources" not in tables:
        op.create_table(
            "event_sources",
            sa.Column("key", sa.String(80), primary_key=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("secret", sa.String(128), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )

    # triggers table
    if "triggers" not in tables:
        op.create_table(
            "triggers",
            sa.Column("key", sa.String(80), primary_key=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("source_key", sa.String(80), nullable=False),
            sa.Column("event_type", sa.String(64), nullable=False),
            sa.Column(
                "target_type",
                sa.String(32),
                nullable=False,
            ),
            sa.Column("definition_key", sa.String(80), nullable=True),
            sa.Column("mapping_json", sa.Text(), nullable=False),
            sa.Column(
                "schedule_kind",
                sa.String(16),
                nullable=False,
                server_default="webhook",
            ),
            sa.Column("interval_seconds", sa.Integer(), nullable=True),
            sa.Column("daily_at_utc", sa.String(5), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_triggers_source_key", "triggers", ["source_key"]
        )
        op.create_index(
            "ix_triggers_active", "triggers", ["active"]
        )

    # trigger_deliveries table
    if "trigger_deliveries" not in tables:
        op.create_table(
            "trigger_deliveries",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("trigger_key", sa.String(80), nullable=False),
            sa.Column("event_id", sa.String(128), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column(
                "status",
                sa.String(16),
                nullable=False,
                server_default="received",
            ),
            sa.Column("request_id", sa.String(64), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint(
                "trigger_key", "event_id", name="uq_trigger_delivery_event"
            ),
        )
        op.create_index(
            "ix_trigger_deliveries_trigger_key", "trigger_deliveries", ["trigger_key"]
        )
        op.create_index(
            "ix_trigger_deliveries_status", "trigger_deliveries", ["status"]
        )


def downgrade() -> None:
    """No-op: keep trigger history."""
    pass
