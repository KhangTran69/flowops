"""PostgreSQL coverage for notification dead-letter and requeue transitions.

Skipped unless ``FLOWOPS_TEST_POSTGRES_URL`` points at a disposable database.
CI runs this module with the idempotency PostgreSQL tests.
"""

import json
import os

import pytest
from fastapi.testclient import TestClient

from src import api, db
from src.notify import DeliveryResult

POSTGRES_URL = os.environ.get("FLOWOPS_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL, reason="FLOWOPS_TEST_POSTGRES_URL is not set"
)

VALID_REQUEST = {
    "userId": 7,
    "id": 42,
    "title": "Laptop will not start",
    "completed": False,
}


@pytest.fixture
def client():
    db.use_database(POSTGRES_URL)
    db.Base.metadata.drop_all(bind=db.engine)
    db.Base.metadata.create_all(bind=db.engine)
    try:
        yield TestClient(api.app)
    finally:
        db.Base.metadata.drop_all(bind=db.engine)


class ScriptedDelivery:
    """Small offline replacement for deliver_webhook."""

    def __init__(self, results):
        self.results = list(results)

    def __call__(self, url, payload, **kwargs):
        return DeliveryResult(**self.results.pop(0))


def _failed_delivery(attempts=1):
    return {"success": False, "attempts": attempts, "last_error": "boom"}


def _successful_delivery(attempts=1):
    return {"success": True, "attempts": attempts, "last_error": None}


def test_postgres_requeue_failed_delivery_is_conditional(client):
    session = db.SessionLocal()
    try:
        record = db.NotificationOutboxRecord(
            url="https://hooks.example/pg",
            payload_json=json.dumps({"event": "ticket.created"}),
            ticket_id="42",
            status="failed",
            attempts=5,
            last_error="boom",
        )
        session.add(record)
        session.commit()
        notification_id = record.id
    finally:
        session.close()

    first = client.post(f"/notifications/{notification_id}/requeue")
    assert first.status_code == 200
    assert first.json()["status"] == "pending"
    assert first.json()["attempts"] == 5
    assert first.json()["last_error"] == "boom"
    assert client.get("/notifications/failed").json() == []

    second = client.post(f"/notifications/{notification_id}/requeue")
    assert second.status_code == 409
    logs = client.get("/execution-logs").json()
    assert any(log["action"] == "notification.requeued" for log in logs)


def test_postgres_retry_dead_letters_then_requeue_delivers(client, monkeypatch):
    fake = ScriptedDelivery(
        [
            _failed_delivery(attempts=1),  # initial ticket notification
            _failed_delivery(attempts=1),  # manual retry -> dead-letter
            _successful_delivery(attempts=1),  # after requeue
        ]
    )
    monkeypatch.setattr(api, "deliver_webhook", fake)
    monkeypatch.setenv("FLOWOPS_NOTIFICATION_WEBHOOK_URL", "https://hooks.example/pg")
    monkeypatch.setenv("FLOWOPS_NOTIFICATION_MAX_ATTEMPTS", "1")

    assert client.post("/tickets", json=VALID_REQUEST).status_code == 201
    pending = client.get("/notifications/pending").json()
    assert len(pending) == 1

    retried = client.post("/notifications/retry").json()
    assert [entry["status"] for entry in retried] == ["failed"]
    assert retried[0]["attempts"] == 2
    failed = client.get("/notifications/failed").json()
    assert len(failed) == 1
    logs = client.get("/execution-logs").json()
    assert any(log["action"] == "notification.dead_lettered" for log in logs)

    requeued = client.post(f"/notifications/{failed[0]['id']}/requeue")
    assert requeued.status_code == 200
    assert requeued.json()["status"] == "pending"

    delivered = client.post("/notifications/retry").json()
    assert [entry["status"] for entry in delivered] == ["delivered"]
    assert client.get("/notifications/failed").json() == []
    assert client.get("/notifications/pending").json() == []
