"""Create durable UI session revocation storage."""
from alembic import op
import sqlalchemy as sa

revision = "0017_ui_session_revocation"
down_revision = "0016_manual_ticket_fields"
branch_labels = None
depends_on = None


def upgrade():
    if not sa.inspect(op.get_bind()).has_table("revoked_ui_sessions"):
        op.create_table("revoked_ui_sessions",
                        sa.Column("digest", sa.String(64), primary_key=True),
                        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
        op.create_index("ix_revoked_ui_sessions_expires_at", "revoked_ui_sessions", ["expires_at"])


def downgrade():
    op.drop_table("revoked_ui_sessions")
