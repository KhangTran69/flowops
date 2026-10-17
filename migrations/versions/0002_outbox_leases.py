"""Add dispatcher lease fields to notification_outbox.

Revision ID: 0002_outbox_leases
Revises: 0001_initial_schema
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_outbox_leases"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("notification_outbox")}
    additions = {
        "lease_owner_token": sa.Column("lease_owner_token", sa.String(64)),
        "lease_expires_at": sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        "next_attempt_at": sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
    }
    for name, column in additions.items():
        if name not in columns:
            op.add_column("notification_outbox", column)
    index_names = {
        index["name"] for index in inspector.get_indexes("notification_outbox")
    }
    if "ix_notification_outbox_status_next" not in index_names:
        op.create_index(
            "ix_notification_outbox_status_next",
            "notification_outbox",
            ["status", "next_attempt_at"],
        )


def downgrade() -> None:
    """No-op: keep delivery data and leases in place."""
    pass
