"""Add DAG checklist columns and ChatOps token-use ledger.

Revision ID: 0006_workflow_dag_chatops
Revises: 0005_execution_correlation
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_workflow_dag_chatops"
down_revision: Union[str, None] = "0005_execution_correlation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "checklist_items" in tables:
        columns = {column["name"] for column in inspector.get_columns("checklist_items")}
        if "depends_on_json" not in columns:
            op.add_column("checklist_items", sa.Column("depends_on_json", sa.Text()))
        if "output_json" not in columns:
            op.add_column("checklist_items", sa.Column("output_json", sa.Text()))
    if "chatops_token_uses" not in tables:
        op.create_table(
            "chatops_token_uses",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("jti", sa.String(64), nullable=False, unique=True),
            sa.Column("request_id", sa.Integer(), nullable=False),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("action", sa.String(16), nullable=False),
            sa.Column("actor_subject", sa.String(120), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            "ix_chatops_token_uses_jti", "chatops_token_uses", ["jti"], unique=True
        )


def downgrade() -> None:
    """No-op: keep DAG/output data."""
    pass
