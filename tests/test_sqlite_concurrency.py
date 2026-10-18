"""SQLite must tolerate concurrent writers, or multi-worker deployments break.

Without WAL and a busy timeout, two workers writing at the same moment raise
``sqlite3.OperationalError: database is locked``. These tests pin that behaviour
so a future engine change cannot silently reintroduce it.
"""

import threading

from sqlalchemy import text

from src import db


def test_sqlite_uses_wal_and_a_busy_timeout(tmp_path):
    db.use_database(f"sqlite:///{tmp_path}/pragmas.db")
    db._ensure_tables()

    with db.engine.connect() as connection:
        journal_mode = connection.execute(text("PRAGMA journal_mode")).scalar()
        busy_timeout = connection.execute(text("PRAGMA busy_timeout")).scalar()

    assert str(journal_mode).lower() == "wal"
    assert int(busy_timeout) >= 1000


def test_concurrent_writers_do_not_raise_database_is_locked(tmp_path):
    db.use_database(f"sqlite:///{tmp_path}/writers.db")
    db._ensure_tables()

    errors: list[BaseException] = []
    rows_per_thread = 15
    thread_count = 4

    def write(batch: int) -> None:
        for index in range(rows_per_thread):
            session = db.SessionLocal()
            try:
                session.add(
                    db.TicketRecord(
                        ticket_id=f"T-{batch}-{index}",
                        requester_id=1,
                        title="concurrent write",
                        status="open",
                        priority="low",
                    )
                )
                session.commit()
            except BaseException as error:  # noqa: BLE001 - the point of the test
                errors.append(error)
            finally:
                session.close()

    threads = [threading.Thread(target=write, args=(batch,)) for batch in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"concurrent writers failed: {errors[:2]}"
    session = db.SessionLocal()
    try:
        count = session.query(db.TicketRecord).count()
    finally:
        session.close()
    assert count == thread_count * rows_per_thread
