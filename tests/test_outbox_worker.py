"""Tests for the automatic, leased notification-outbox dispatcher."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from src import db, outbox, outbox_worker
from src.notify import DeliveryResult


@pytest.fixture
def database(tmp_path):
    db.use_database(f"sqlite:///{tmp_path}/outbox_worker.db")
    db._ensure_tables()
    yield


def _insert(status="pending", attempts=0):
    session = db.SessionLocal()
    try:
        record = db.NotificationOutboxRecord(
            url="https://hooks.example/dispatch",
            payload_json='{"event":"ticket.created"}',
            ticket_id="42",
            status=status,
            attempts=attempts,
            last_error="old error" if attempts else None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(record)
        session.commit()
        return record.id
    finally:
        session.close()


def _row(notification_id):
    session = db.SessionLocal()
    try:
        return session.get(db.NotificationOutboxRecord, notification_id)
    finally:
        session.close()


def test_dispatcher_delivers_pending_row(database, monkeypatch):
    notification_id = _insert()
    calls = []

    def fake_deliver(url, payload, **kwargs):
        calls.append((url, payload))
        return DeliveryResult(success=True, attempts=1, last_error=None)

    monkeypatch.setattr(outbox_worker, "deliver_webhook", fake_deliver)
    processed = outbox_worker.run_once(worker_id="outbox-test")
    assert processed is not None
    assert processed.id == notification_id
    assert processed.status == "delivered"
    assert processed.attempts == 1
    assert calls
    logs = outbox_worker.db.SessionLocal()
    try:
        actions = [
            row.action
            for row in logs.execute(select(db.ExecutionLogRecord)).scalars()
        ]
    finally:
        logs.close()
    assert "notification.dispatched" in actions


def test_dispatcher_retries_with_backoff_then_dead_letters(database, monkeypatch):
    monkeypatch.setenv("FLOWOPS_NOTIFICATION_MAX_ATTEMPTS", "2")
    notification_id = _insert(attempts=0)

    def failing(url, payload, **kwargs):
        return DeliveryResult(success=False, attempts=1, last_error="boom")

    monkeypatch.setattr(outbox_worker, "deliver_webhook", failing)
    first = outbox_worker.run_once(worker_id="outbox-test")
    assert first is not None
    assert first.status == "pending"
    assert first.attempts == 1
    assert first.next_attempt_at is not None
    due = first.next_attempt_at
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    assert due > datetime.now(timezone.utc)

    # Not due yet.
    assert outbox_worker.run_once(worker_id="outbox-test") is None

    # Make it due and fail once more -> dead letter.
    session = db.SessionLocal()
    try:
        row = session.get(db.NotificationOutboxRecord, notification_id)
        row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
    finally:
        session.close()
    second = outbox_worker.run_once(worker_id="outbox-test")
    assert second is not None
    assert second.status == "failed"
    assert second.attempts == 2


def test_stale_dispatcher_cannot_finalize_takeover(database):
    notification_id = _insert()
    session = db.SessionLocal()
    try:
        first = outbox.claim_delivery(session, "outbox-a", lease_seconds=1)
        assert first is not None and first.id == notification_id
        stale_token = first.lease_owner_token
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        second = outbox.claim_delivery(session, "outbox-b", now=future)
        assert second is not None and second.id == notification_id
        current_token = second.lease_owner_token
        assert current_token != stale_token

        assert outbox.finalize_delivery(
            session,
            notification_id,
            stale_token or "",
            success=True,
            attempt_delta=1,
            error=None,
            max_attempts=5,
        ) is None
        assert outbox.finalize_delivery(
            session,
            notification_id,
            current_token or "",
            success=True,
            attempt_delta=1,
            error=None,
            max_attempts=5,
        ) == "delivered"
        session.commit()
    finally:
        session.close()


def test_concurrent_claims_only_one_owner(database):
    notification_id = _insert()
    session = db.SessionLocal()
    try:
        first = outbox.claim_delivery(session, "outbox-a", lease_seconds=60)
        second = outbox.claim_delivery(session, "outbox-b", lease_seconds=60)
        assert first is not None and first.id == notification_id
        assert second is None
    finally:
        session.close()
