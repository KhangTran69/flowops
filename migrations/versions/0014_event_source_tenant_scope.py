"""Add tenant ownership to legacy webhook event sources.

Revision ID: 0014_event_source_tenant_scope
Revises: 0013_event_connector_identity
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_event_source_tenant_scope"
down_revision: Union[str, None] = "0013_event_connector_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("event_sources")}
    if "tenant_key" not in columns:
        op.add_column("event_sources", sa.Column("tenant_key", sa.String(64), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("event_sources")}
    if "tenant_key" in columns:
        op.drop_column("event_sources", "tenant_key")
