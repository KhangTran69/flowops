"""Add optional manual intake description and contact email."""
from alembic import op
import sqlalchemy as sa

revision = "0016_manual_ticket_fields"
down_revision = "0015_event_source_scope_rotation"
branch_labels = None
depends_on = None


def upgrade():
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("tickets")}
    if "description" not in existing:
        op.add_column("tickets", sa.Column("description", sa.Text(), nullable=True))
    if "requester_email" not in existing:
        op.add_column("tickets", sa.Column("requester_email", sa.String(254), nullable=True))


def downgrade():
    with op.batch_alter_table("tickets") as batch:
        batch.drop_column("requester_email")
        batch.drop_column("description")
