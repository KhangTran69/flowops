"""Add the ticket queue reporting view.

Revision ID: 0009_reporting_view
Revises: 0008_workflow_definitions
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_reporting_view"
down_revision: Union[str, None] = "0008_workflow_definitions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

VIEW_SQL = """
CREATE VIEW flowops_ticket_queue_summary AS
SELECT
  COALESCE(queue, 'unassigned') AS queue,
  priority,
  status,
  COUNT(*) AS ticket_count
FROM tickets
GROUP BY COALESCE(queue, 'unassigned'), priority, status
"""

FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION flowops_ticket_queue_summary()
RETURNS TABLE(queue text, priority text, status text, ticket_count bigint)
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(queue, 'unassigned')::text,
         priority::text,
         status::text,
         COUNT(*)::bigint
  FROM tickets
  GROUP BY COALESCE(queue, 'unassigned'), priority, status
$$
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # A function, not a view: PostgreSQL drop_all() fixtures must be able to
        # drop the tickets table without CASCADE.
        op.execute("DROP FUNCTION IF EXISTS flowops_ticket_queue_summary()")
        op.execute(FUNCTION_SQL)
        return
    if not op.get_context().as_sql:
        inspector = sa.inspect(bind)
        if "flowops_ticket_queue_summary" in inspector.get_view_names():
            return
    op.execute("DROP VIEW IF EXISTS flowops_ticket_queue_summary")
    op.execute(VIEW_SQL)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS flowops_ticket_queue_summary()")
    op.execute("DROP VIEW IF EXISTS flowops_ticket_queue_summary")
