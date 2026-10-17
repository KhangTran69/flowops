"""Add correlation IDs to execution logs.

Revision ID: 0005_execution_correlation
Revises: 0004_offboarding_claim
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_execution_correlation"
down_revision: Union[str, None] = "0004_offboarding_claim"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "execution_logs" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("execution_logs")}
    if "correlation_id" not in columns:
        op.add_column("execution_logs", sa.Column("correlation_id", sa.String(64)))


def downgrade() -> None:
    """No-op: keep log correlation data."""
    pass
