"""Add tenants, external links, tenant_key columns and reporting views.

Revision ID: 0009_tenants_reporting
Revises: 0008_workflow_definitions
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from src import reports

revision: str = "0009_tenants_reporting"
down_revision: Union[str, None] = "0008_workflow_definitions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "tenants" not in tables:
        op.create_table(
            "tenants",
            sa.Column("key", sa.String(64), primary_key=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("entra_tenant_id", sa.String(64)),
            sa.Column("default_domain", sa.String(255)),
            sa.Column("licence_sku", sa.String(120)),
            sa.Column("usage_location", sa.String(2)),
            sa.Column("psa_company_id", sa.String(64)),
            sa.Column("credential_ref", sa.String(64)),
            sa.Column("baseline_json", sa.Text()),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )
    if "external_links" not in tables:
        op.create_table(
            "external_links",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("system", sa.String(32), nullable=False),
            sa.Column("local_type", sa.String(32), nullable=False),
            sa.Column("local_id", sa.String(64), nullable=False),
            sa.Column("external_id", sa.String(64), nullable=False),
            sa.Column("tenant_key", sa.String(64)),
            sa.Column("last_synced_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("system", "local_type", "local_id", name="uq_external_link_local"),
            sa.UniqueConstraint("system", "local_type", "external_id", name="uq_external_link_remote"),
        )
    for table in ("tickets", "onboarding_requests"):
        columns = {c["name"] for c in inspector.get_columns(table)}
        if "tenant_key" not in columns:
            op.add_column(table, sa.Column("tenant_key", sa.String(64)))
        indexes = {i["name"] for i in inspector.get_indexes(table)}
        if f"ix_{table}_tenant_key" not in indexes:
            op.create_index(f"ix_{table}_tenant_key", table, ["tenant_key"])
    reports.create_views(bind)


def downgrade() -> None:
    """Drop only the derived views/function; keep tenant data."""
    reports.drop_views(op.get_bind())
