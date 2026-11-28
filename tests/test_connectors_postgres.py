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


def test_postgres_concurrent_poll_claims_fetch_once(database, monkeypatch):
    from datetime import datetime, timezone
    from threading import Event

    with db.SessionLocal() as session:
        session.add(db.ConnectorRecord(name="Synthetic polling source", connector_type="jira",
            kind="polling", enabled=True, config_json="{}"))
        session.commit()
    entered, release = Event(), Event()
    calls = []

    class FakeConnector:
        def fetch_events(self, session):
            calls.append(1)
            entered.set()
            assert release.wait(timeout=10)
            return [connectors.NormalizedEvent("jira", "issue_updated", "one", {})]

    monkeypatch.setattr(connectors, "build_connector", lambda *args, **kwargs: FakeConnector())
    at = datetime.now(timezone.utc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(connectors.poll_all_connectors, at)
        try:
            assert entered.wait(timeout=10)
            second = pool.submit(connectors.poll_all_connectors, at)
            assert second.result(timeout=10) == 0
        finally:
            release.set()
        assert first.result(timeout=10) == 1
    assert calls == [1]
    assert connectors.poll_all_connectors(at) == 0


def test_page_checkpoint_replays_after_partial_failure(database, monkeypatch):
    import json
    from datetime import datetime, timezone

    with db.SessionLocal() as session:
        record = db.ConnectorRecord(name="Paged", connector_type="jira", kind="polling",
            enabled=True, config_json="{}")
        session.add(record)
        session.commit()
        record_id = record.id
    calls = []

    class Paged:
        def fetch_page(self, session, cursor):
            calls.append(cursor)
            if cursor is None:
                return [connectors.NormalizedEvent("jira", "issue_updated", key, {}) for key in ("a", "b")], "next"
            return [connectors.NormalizedEvent("jira", "issue_updated", "c", {})], None

    real_ingest = connectors.ingest_event
    failed = False

    def flaky(session, event):
        nonlocal failed
        if event.external_id == "b" and not failed:
            failed = True
            raise RuntimeError("synthetic page failure")
        return real_ingest(session, event)

    monkeypatch.setattr(connectors, "build_connector", lambda *a, **kw: Paged())
    monkeypatch.setattr(connectors, "ingest_event", flaky)
    at = datetime.now(timezone.utc)
    assert connectors.poll_all_connectors(at, only_due=False) == 1
    with db.SessionLocal() as session:
        assert json.loads(session.get(db.ConnectorRecord, record_id).cursor_json)["cursor"] is None
    assert connectors.poll_all_connectors(at, only_due=False) == 1
    with db.SessionLocal() as session:
        assert json.loads(session.get(db.ConnectorRecord, record_id).cursor_json)["cursor"] == "next"
    assert connectors.poll_all_connectors(at, only_due=False) == 1
    assert calls == [None, None, "next"]
    with db.SessionLocal() as session:
        assert json.loads(session.get(db.ConnectorRecord, record_id).cursor_json)["cursor"] is None
        assert len(session.execute(select(db.EventRecord)).scalars().all()) == 3
