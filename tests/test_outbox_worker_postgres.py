"""PostgreSQL coverage for the leased outbox dispatcher."""

import os
from datetime import datetime, timedelta, timezone

import pytest

from src import db, outbox, outbox_worker
from src.notify import DeliveryResult

POSTGRES_URL = os.environ.get("FLOWOPS_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL, reason="FLOWOPS_TEST_POSTGRES_URL is not set"
)


@pytest.fixture
def database():
    db.use_database(POSTGRES_URL)
    db.Base.metadata.drop_all(bind=db.engine)
    db.Base.metadata.create_all(bind=db.engine)
    yield
    db.Base.metadata.drop_all(bind=db.engine)


def _insert():
    session = db.SessionLocal()
    try:
        row = db.NotificationOutboxRecord(
            url="https://hooks.example/pg-outbox",
            payload_json='{"event":"ticket.created"}',
            ticket_id="42",
            status="pending",
            attempts=0,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(row)
        session.commit()
        return row.id
    finally:
        session.close()


def test_postgres_outbox_takeover_and_dispatcher(database, monkeypatch):
    notification_id = _insert()
    session = db.SessionLocal()
    try:
        first = outbox.claim_delivery(session, "outbox-a", lease_seconds=1)
        assert first is not None
        stale_token = first.lease_owner_token
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        second = outbox.claim_delivery(session, "outbox-b", now=future)
        assert second is not None
        current_token = second.lease_owner_token
        assert current_token != stale_token
        assert outbox.finalize_delivery(
            session, notification_id, stale_token or "",
            success=True, attempt_delta=1, error=None, max_attempts=5,
        ) is None
        session.rollback()
    finally:
        session.close()

    def fake_deliver(url, payload, **kwargs):
        return DeliveryResult(success=True, attempts=1, last_error=None)

    monkeypatch.setattr(outbox_worker, "deliver_webhook", fake_deliver)
    # Make the row due again for the dispatcher.
    session = db.SessionLocal()
    try:
        row = session.get(db.NotificationOutboxRecord, notification_id)
        row.lease_owner_token = None
        row.lease_expires_at = None
        row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
    finally:
        session.close()
    processed = outbox_worker.run_once(worker_id="pg-outbox")
    assert processed is not None
    assert processed.status == "delivered"
