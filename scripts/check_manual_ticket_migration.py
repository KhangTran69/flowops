"""Migration rollback smoke check for disposable CI PostgreSQL databases only."""
import os
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import command
from alembic.config import Config


def main():
    if os.environ.get("FLOWOPS_DISPOSABLE_MIGRATION_CHECK") != "1":
        raise RuntimeError("This check requires an explicitly disposable database.")
    url = os.environ["FLOWOPS_DATABASE_URL"]
    if not url.startswith("postgresql"):
        raise RuntimeError("PostgreSQL is required.")
    config = Config("alembic.ini")
    engine = sa.create_engine(url)
    ticket_id = "99000001"
    try:
        command.upgrade(config, "head")
        with engine.begin() as connection:
            tickets = sa.Table("tickets", sa.MetaData(), autoload_with=connection)
            now = datetime.now(timezone.utc)
            connection.execute(tickets.insert().values(
                ticket_id=ticket_id, requester_id=7, title="Migration fixture",
                status="open", priority="normal", description="temporary metadata",
                requester_email="migration@example.test", created_at=now, updated_at=now,
            ))
        command.downgrade(config, "0015_event_source_scope_rotation")
        with engine.connect() as connection:
            columns = {column["name"] for column in sa.inspect(connection).get_columns("tickets")}
            assert "description" not in columns and "requester_email" not in columns
            assert connection.execute(
                sa.text("SELECT title FROM tickets WHERE ticket_id=:id"), {"id": ticket_id}
            ).scalar_one() == "Migration fixture"
        command.upgrade(config, "head")
        with engine.begin() as connection:
            row = connection.execute(
                sa.text("SELECT title, description, requester_email FROM tickets WHERE ticket_id=:id"),
                {"id": ticket_id},
            ).one()
            assert tuple(row) == ("Migration fixture", None, None)
            connection.execute(sa.text("DELETE FROM tickets WHERE ticket_id=:id"), {"id": ticket_id})
        print("MIGRATION_ROUNDTRIP: PASS; legacy ticket retained; downgraded optional metadata is intentionally lost.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
