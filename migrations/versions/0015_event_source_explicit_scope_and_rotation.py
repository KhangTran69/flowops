"""Require explicit source scope and generated webhook secrets.

Revision ID: 0015_event_source_scope_rotation
Revises: 0014_event_source_tenant_scope
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_event_source_scope_rotation"
down_revision: Union[str, None] = "0014_event_source_tenant_scope"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("event_sources")
    }
    if "global_scope" not in columns:
        op.add_column(
            "event_sources",
            sa.Column("global_scope", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "secret_generated" not in columns:
        op.add_column(
            "event_sources",
            sa.Column("secret_generated", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("event_sources")
    }
    if "secret_generated" in columns:
        op.drop_column("event_sources", "secret_generated")
    if "global_scope" in columns:
        op.drop_column("event_sources", "global_scope")
