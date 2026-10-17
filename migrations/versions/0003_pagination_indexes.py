"""Add indexes used by pagination and operator filters.

Revision ID: 0003_pagination_indexes
Revises: 0002_outbox_leases
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_pagination_indexes"
down_revision: Union[str, None] = "0002_outbox_leases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEXES = (
    ("ix_execution_logs_status_created", "execution_logs", ["status", "created_at"]),
    ("ix_workflow_jobs_status_available", "workflow_jobs", ["status", "available_at"]),
    (
        "ix_notification_outbox_status_next",
        "notification_outbox",
        ["status", "next_attempt_at"],
    ),
    ("ix_onboarding_requests_status", "onboarding_requests", ["status"]),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for name, table, columns in INDEXES:
        if table not in inspector.get_table_names():
            continue
        existing = {index["name"] for index in inspector.get_indexes(table)}
        if name not in existing:
            op.create_index(name, table, columns)


def downgrade() -> None:
    """No-op: indexes are safe to keep on rollback."""
    pass
