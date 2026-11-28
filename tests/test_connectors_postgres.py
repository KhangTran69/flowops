"""PostgreSQL connector concurrency regressions on disposable test database."""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select

from src import db
from src.services import connectors

POSTGRES_URL = os.environ.get("FLOWOPS_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="FLOWOPS_TEST_POSTGRES_URL is not set")


@pytest.fixture
def database():
    db.use_database(POSTGRES_URL)
    db.Base.metadata.drop_all(bind=db.engine)
    db.Base.metadata.create_all(bind=db.engine)
    try:
        yield
    finally:
        db.Base.metadata.drop_all(bind=db.engine)


def test_concurrent_event_insert_returns_duplicate(database):
    gate = Barrier(2)
    event = connectors.NormalizedEvent("jira", "issue_updated", "42", {})
    def ingest(_):
        with db.SessionLocal() as session:
            gate.wait(timeout=10)
            result = connectors.ingest_event(session, event)
            session.commit()
            return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(ingest, range(2)))
    assert sorted(row["duplicate"] for row in results) == [False, True]
    assert len({row["event_id"] for row in results}) == 1
    with db.SessionLocal() as session:
        assert len(session.execute(select(db.EventRecord)).scalars().all()) == 1
