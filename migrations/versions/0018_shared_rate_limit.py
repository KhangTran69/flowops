"""Add shared PostgreSQL rate limit counters."""
from alembic import op
import sqlalchemy as sa

revision = "0018_shared_rate_limit"
down_revision = "0017_ui_session_revocation"
branch_labels = None
depends_on = None


def upgrade():
    if not sa.inspect(op.get_bind()).has_table("rate_limit_buckets"):
        op.create_table("rate_limit_buckets",
                        sa.Column("digest", sa.String(64), primary_key=True),
                        sa.Column("window_id", sa.Integer(), nullable=False),
                        sa.Column("count", sa.Integer(), nullable=False))
        op.create_index("ix_rate_limit_buckets_window_id", "rate_limit_buckets", ["window_id"])


def downgrade():
    op.drop_table("rate_limit_buckets")
