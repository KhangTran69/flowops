"""Add claim/lease fields to the offboarding completion ledger.

Revision ID: 0004_offboarding_claim
Revises: 0003_pagination_indexes
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_offboarding_claim"
down_revision: Union[str, None] = "0003_pagination_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "offboarding_completions" not in inspector.get_table_names():
        return
    columns = {
        column["name"]
        for column in inspector.get_columns("offboarding_completions")
    }
    additions = {
        "status": sa.Column("status", sa.String(16)),
        "owner_token": sa.Column("owner_token", sa.String(64)),
        "claimed_at": sa.Column("claimed_at", sa.DateTime(timezone=True)),
    }
    for name, column in additions.items():
        if name not in columns:
            op.add_column("offboarding_completions", column)


def downgrade() -> None:
    """No-op: keep ledger claim data."""
    pass
