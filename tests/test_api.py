"""Tests for the FlowOps API: intake, routing, idempotency, webhooks, logs.

Fully offline: SQLite in a temp directory, HTTP via TestClient.
"""

import json

import pytest
from fastapi.testclient import TestClient

from src import api, db

VALID_REQUEST = {
    "userId": 7,
    "id": 42,
    "title": "Laptop will not start",
    "completed": False,
}


@pytest.fixture
def client(tmp_path):
    db.use_database(f"sqlite:///{tmp_path}/test.db")
    return TestClient(api.app)


# --- Service routes ---


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_root_returns_html_landing_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "FlowOps" in response.text
    assert "/docs" in response.text


# --- Ticket intake ---


def test_valid_ticket_is_routed_and_stored(client):
    response = client.post("/tickets", json=VALID_REQUEST)
    assert response.status_code == 201
    assert response.json() == {
        "ticket_id": "42",
        "requester_id": 7,
        "title": "Laptop will not start",
        "status": "open",
        "priority": "normal",
        "category": "hardware",
        "queue": "it-hardware",
        # Ticket closing fields (v1.0 preview); set only via POST /tickets/42/close.
        "resolution_note": None,
        "closed_at": None,
    }


def test_completed_ticket_maps_to_closed(client):
    response = client.post("/tickets", json={**VALID_REQUEST, "completed": True})
    assert response.json()["status"] == "closed"


def test_urgent_network_ticket_routed_correctly(client):
    response = client.post(
        "/tickets",
        json={**VALID_REQUEST, "id": 43, "title": "VPN outage — all users cannot work"},
    )
    body = response.json()
    assert body["category"] == "network"
    assert body["priority"] == "urgent"
    assert body["queue"] == "it-network"


def test_resubmitting_same_id_updates_ticket(client):
    first = client.post("/tickets", json=VALID_REQUEST)
    second = client.post("/tickets", json={**VALID_REQUEST, "title": "Updated title"})
    assert (first.status_code, second.status_code) == (201, 200)
    assert client.get("/tickets/42").json()["title"] == "Updated title"


@pytest.mark.parametrize(
    "payload",
    [
        {k: v for k, v in VALID_REQUEST.items() if k != "userId"},  # missing field
        {**VALID_REQUEST, "userId": "7"},  # string instead of int
        {**VALID_REQUEST, "id": True},  # bool instead of int
        {**VALID_REQUEST, "completed": 1},  # int instead of bool
        {**VALID_REQUEST, "title": 123},  # int instead of str
        {**VALID_REQUEST, "title": ""},  # empty title
        {**VALID_REQUEST, "title": "   "},  # whitespace-only title
        {**VALID_REQUEST, "id": 0},  # non-positive id
        ["not", "an", "object"],  # wrong top-level structure
    ],
)
def test_invalid_requests_rejected_with_422(client, payload):
    assert client.post("/tickets", json=payload).status_code == 422


def test_get_ticket_returns_stored_ticket(client):
    client.post("/tickets", json=VALID_REQUEST)
    response = client.get("/tickets/42")
    assert response.status_code == 200
    assert response.json()["ticket_id"] == "42"


def test_get_unknown_ticket_returns_404(client):
    assert client.get("/tickets/999").status_code == 404


def test_get_non_numeric_ticket_id_returns_404(client):
    # Non-digit IDs (including path traversal attempts) must never resolve
    # to a file path or database lookup.
    assert client.get("/tickets/not-a-number").status_code == 404
    assert client.get("/tickets/%2e%2e").status_code == 404


def test_list_tickets_sorted_by_id(client):
    for ticket_id in (17, 3, 8):
        client.post("/tickets", json={**VALID_REQUEST, "id": ticket_id})
    ids = [ticket["ticket_id"] for ticket in client.get("/tickets").json()]
    assert ids == ["3", "8", "17"]


# --- Idempotency (v0.5) ---


def test_idempotency_key_replays_stored_response(client):
    headers = {"Idempotency-Key": "key-123"}
    first = client.post("/tickets", json=VALID_REQUEST, headers=headers)
    second = client.post("/tickets", json=VALID_REQUEST, headers=headers)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.headers["Idempotency-Replayed"] == "true"
    assert second.json() == first.json()
    # The replay must not create a duplicate ticket or a second create-run.
    assert len(client.get("/tickets").json()) == 1
    assert client.get("/execution-logs").json()[0]["action"] == "ticket.idempotent_replay"


def test_different_idempotency_keys_create_separate_tickets(client):
    client.post("/tickets", json=VALID_REQUEST, headers={"Idempotency-Key": "a"})
    client.post(
        "/tickets", json={**VALID_REQUEST, "id": 43}, headers={"Idempotency-Key": "b"}
    )
    assert len(client.get("/tickets").json()) == 2


def test_overlong_idempotency_key_rejected(client):
    response = client.post(
        "/tickets", json=VALID_REQUEST, headers={"Idempotency-Key": "x" * 129}
    )
    assert response.status_code == 422


def test_same_payload_same_key_replays_without_reexecuting(client):
    """Replaying the exact same payload + key returns the stored result.

    The workflow must not run again: no second ticket row, no second
    create/update log — only a `ticket.idempotent_replay` log entry.
    """
    headers = {"Idempotency-Key": "replay-key"}
    first = client.post("/tickets", json=VALID_REQUEST, headers=headers)
    actions_after_first = [log["action"] for log in client.get("/execution-logs").json()]

    replay = client.post("/tickets", json=VALID_REQUEST, headers=headers)

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()
    # Nothing was re-executed: still one ticket, and the only new log is the replay.
    assert len(client.get("/tickets").json()) == 1
    actions_after_replay = [log["action"] for log in client.get("/execution-logs").json()]
    assert actions_after_replay == ["ticket.idempotent_replay"] + actions_after_first


def test_different_payload_same_key_returns_409_without_reexecuting(client):
    """A key bound to one payload must reject a different payload with 409."""
    headers = {"Idempotency-Key": "conflict-key"}
    client.post("/tickets", json=VALID_REQUEST, headers=headers)
    actions_before_conflict = [
        log["action"] for log in client.get("/execution-logs").json()
    ]

    conflict = client.post(
        "/tickets", json={**VALID_REQUEST, "title": "Changed title"}, headers=headers
    )

    assert conflict.status_code == 409
    assert "different request" in conflict.json()["detail"]
    # The rejected request must not execute: original ticket untouched, no new logs.
    assert client.get("/tickets/42").json()["title"] == VALID_REQUEST["title"]
    assert [log["action"] for log in client.get("/execution-logs").json()] == (
        actions_before_conflict
    )


def test_legacy_idempotency_record_without_fingerprint_fails_safe(client):
    """Records saved before payload binding have no `request_hash`.

    Reusing such a key must return 409 (fail safe: we cannot prove the
    payload matches) instead of replaying or silently re-executing.
    """
    legacy_ticket = {
        "ticket_id": "42",
        "requester_id": 7,
        "title": "Laptop will not start",
        "status": "open",
        "priority": "normal",
        "category": "hardware",
        "queue": "it-hardware",
    }
    legacy_payload = {"ticket": legacy_ticket}
    # Create the schema up front (normally done lazily on the first request),
    # then insert a pre-binding record directly, as an old deployment would have.
    db.Base.metadata.create_all(db.engine)
    with db.SessionLocal() as session:
        session.add(
            db.IdempotencyRecord(
                key="legacy-key",
                ticket_id="42",
                response_json=json.dumps(legacy_payload),
            )
        )
        session.commit()

    response = client.post(
        "/tickets", json=VALID_REQUEST, headers={"Idempotency-Key": "legacy-key"}
    )

    assert response.status_code == 409
    # Fail-safe: nothing executed, so the tickets table stays empty.
    assert client.get("/tickets").json() == []


# --- Webhook intake (v0.4) ---


def test_webhook_intake_accepts_single_ticket(client):
    response = client.post("/webhooks/intake", json={**VALID_REQUEST, "id": 50})
    assert response.status_code == 200
    assert response.json()["ticket_id"] == "50"


def test_webhook_intake_accepts_bulk_list(client):
    response = client.post(
        "/webhooks/intake",
        json=[
            {**VALID_REQUEST, "id": 51},
            {**VALID_REQUEST, "id": 52, "completed": True},
        ],
    )
    assert response.status_code == 200
    assert [ticket["ticket_id"] for ticket in response.json()] == ["51", "52"]
    ids = [ticket["ticket_id"] for ticket in client.get("/tickets").json()]
    assert ids == ["51", "52"]


def test_webhook_intake_rejects_invalid_item(client):
    response = client.post(
        "/webhooks/intake", json=[VALID_REQUEST, {**VALID_REQUEST, "id": "no"}]
    )
    assert response.status_code == 422
    assert client.get("/tickets").json() == []  # nothing stored on 422


# --- Execution logs (v0.3) ---


def test_execution_logs_recorded_for_intake(client):
    client.post("/tickets", json=VALID_REQUEST)
    logs = client.get("/execution-logs").json()
    assert any(
        log["action"] == "ticket.created" and log["status"] == "success" for log in logs
    )
    assert all(log["run_id"] for log in logs)


def test_execution_logs_filter_by_status(client):
    client.post("/tickets", json=VALID_REQUEST)
    assert client.get("/execution-logs", params={"status": "failure"}).json() == []
    successes = client.get("/execution-logs", params={"status": "success"}).json()
    assert successes and all(log["status"] == "success" for log in successes)


def test_notification_failure_is_logged_not_raised(client, monkeypatch):
    calls = []

    def fake_deliver(url, payload, **kwargs):
        calls.append(url)
        from src.notify import DeliveryResult

        return DeliveryResult(success=False, attempts=3, last_error="boom")

    monkeypatch.setattr(api, "deliver_webhook", fake_deliver)
    monkeypatch.setenv("FLOWOPS_NOTIFICATION_WEBHOOK_URL", "https://hooks.example/t")
    response = client.post("/tickets", json=VALID_REQUEST)
    assert response.status_code == 201
    assert calls == ["https://hooks.example/t"]
    failures = client.get("/execution-logs", params={"status": "failure"}).json()
    assert any(log["action"] == "notification.webhook" for log in failures)


# --- Notification outbox: failed webhooks stay visible and retryable ---


class FakeDelivery:
    """Scripted deliver_webhook replacement; records every call (offline)."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, url, payload, **kwargs):
        from src.notify import DeliveryResult

        self.calls.append((url, payload))
        if not self.results:  # fall back to the last scripted outcome
            result = self._last
        else:
            result = self.results.pop(0)
        self._last = result
        return DeliveryResult(**result)


def _failing_delivery(attempts=3, error="boom"):
    return {"success": False, "attempts": attempts, "last_error": error}


def _succeeding_delivery(attempts=1):
    return {"success": True, "attempts": attempts, "last_error": None}


def test_failed_webhook_is_stored_in_outbox(client, monkeypatch):
    fake = FakeDelivery([_failing_delivery()])
    monkeypatch.setattr(api, "deliver_webhook", fake)
    monkeypatch.setenv("FLOWOPS_NOTIFICATION_WEBHOOK_URL", "https://hooks.example/t")
    assert client.post("/tickets", json=VALID_REQUEST).status_code == 201

    pending = client.get("/notifications/pending").json()
    assert len(pending) == 1
    entry = pending[0]
    assert entry["status"] == "pending"
    assert entry["ticket_id"] == "42"
    assert entry["attempts"] == 3
    assert entry["last_error"] == "boom"


def test_successful_webhook_does_not_touch_outbox(client, monkeypatch):
    fake = FakeDelivery([_succeeding_delivery()])
    monkeypatch.setattr(api, "deliver_webhook", fake)
    monkeypatch.setenv("FLOWOPS_NOTIFICATION_WEBHOOK_URL", "https://hooks.example/t")
    assert client.post("/tickets", json=VALID_REQUEST).status_code == 201
    assert client.get("/notifications/pending").json() == []


def test_retry_endpoint_redelivers_pending_and_marks_delivered(client, monkeypatch):
    fake = FakeDelivery([_failing_delivery(), _succeeding_delivery(attempts=1)])
    monkeypatch.setattr(api, "deliver_webhook", fake)
    monkeypatch.setenv("FLOWOPS_NOTIFICATION_WEBHOOK_URL", "https://hooks.example/t")
    client.post("/tickets", json=VALID_REQUEST)

    retried = client.post("/notifications/retry").json()
    assert [entry["status"] for entry in retried] == ["delivered"]
    assert retried[0]["attempts"] == 4  # 3 original + 1 retry attempt
    assert retried[0]["last_error"] is None
    # The retry re-sent the same payload that originally failed.
    assert fake.calls[-1][1] == {
        "event": "ticket.created",
        "ticket": {
            "ticket_id": "42", "requester_id": 7, "title": "Laptop will not start",
            "status": "open", "priority": "normal", "category": "hardware",
            "queue": "it-hardware",
            # Closing fields are part of the ticket shape since v1.0 preview.
            "resolution_note": None, "closed_at": None,
        },
    }
    assert client.get("/notifications/pending").json() == []
    logs = client.get("/execution-logs").json()
    assert any(
        log["action"] == "notification.retry" and log["status"] == "success"
        for log in logs
    )


def test_retry_keeps_still_failing_delivery_pending(client, monkeypatch):
    fake = FakeDelivery([_failing_delivery(), _failing_delivery(attempts=1)])
    monkeypatch.setattr(api, "deliver_webhook", fake)
    monkeypatch.setenv("FLOWOPS_NOTIFICATION_WEBHOOK_URL", "https://hooks.example/t")
    client.post("/tickets", json=VALID_REQUEST)

    retried = client.post("/notifications/retry").json()
    assert retried[0]["status"] == "pending"
    assert retried[0]["attempts"] == 4
    assert retried[0]["last_error"] == "boom"
    assert len(client.get("/notifications/pending").json()) == 1
    logs = client.get("/execution-logs").json()
    assert any(
        log["action"] == "notification.retry" and log["status"] == "failure"
        for log in logs
    )


def test_retry_with_empty_queue_is_a_noop(client, monkeypatch):
    fake = FakeDelivery([])
    monkeypatch.setattr(api, "deliver_webhook", fake)
    assert client.post("/notifications/retry").json() == []
    assert fake.calls == []


def test_dashboard_shows_pending_retries_section(client, monkeypatch):
    fake = FakeDelivery([_failing_delivery()])
    monkeypatch.setattr(api, "deliver_webhook", fake)
    monkeypatch.setenv("FLOWOPS_NOTIFICATION_WEBHOOK_URL", "https://hooks.example/t")
    client.post("/tickets", json=VALID_REQUEST)

    page = client.get("/dashboard").text
    assert "Failed webhook deliveries" in page
    assert "pending webhook retries" in page
    assert "hooks.example" in page  # the failing URL is surfaced


# --- Dashboard (v1.0 preview) ---


def test_dashboard_renders_stats(client):
    client.post("/tickets", json=VALID_REQUEST)
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "FlowOps monitoring dashboard" in response.text
    assert "ticket.created" in response.text


# --- Ticket closing (v1.0 preview) ---

CLOSE_BODY = {"resolution_note": "Replaced the power adapter; laptop boots again."}


def test_close_open_ticket_succeeds(client):
    """open -> closed is the one allowed transition."""
    client.post("/tickets", json=VALID_REQUEST)

    response = client.post("/tickets/42/close", json=CLOSE_BODY)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "closed"
    assert body["resolution_note"] == CLOSE_BODY["resolution_note"]
    assert body["closed_at"] is not None  # timestamp recorded by the API
    # The stored ticket reflects the close on later reads too.
    assert client.get("/tickets/42").json()["status"] == "closed"


def test_double_close_is_rejected_and_preserves_original(client):
    """closed -> closed returns 409 and never overwrites the first close."""
    client.post("/tickets", json=VALID_REQUEST)
    first = client.post("/tickets/42/close", json=CLOSE_BODY).json()

    second = client.post(
        "/tickets/42/close", json={"resolution_note": "Second attempt"}
    )
    assert second.status_code == 409
    assert "already 'closed'" in second.json()["detail"]

    stored = client.get("/tickets/42").json()
    assert stored["resolution_note"] == first["resolution_note"]
    assert stored["closed_at"] == first["closed_at"]


def test_close_nonexistent_ticket_returns_404(client):
    response = client.post("/tickets/999/close", json=CLOSE_BODY)
    assert response.status_code == 404
    assert response.json()["detail"] == "Ticket not found."


def test_close_non_numeric_ticket_id_returns_404(client):
    assert client.post("/tickets/not-a-number/close", json=CLOSE_BODY).status_code == 404


def test_close_ticket_created_as_closed_is_invalid_transition(client):
    """Tickets already closed at intake (completed=true) cannot be closed again."""
    client.post("/tickets", json={**VALID_REQUEST, "completed": True})

    response = client.post("/tickets/42/close", json=CLOSE_BODY)
    assert response.status_code == 409
    assert "only 'open' tickets can be closed" in response.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {},                                        # missing note
        {"resolution_note": ""},                   # empty
        {"resolution_note": "   "},                # blank whitespace
        {"resolution_note": 123},                  # wrong type (strict str)
        {"resolution_note": "x" * 2001},           # too long (max 2000)
    ],
)
def test_close_with_invalid_body_returns_422(client, body):
    client.post("/tickets", json=VALID_REQUEST)
    assert client.post("/tickets/42/close", json=body).status_code == 422


def test_close_logs_success_and_failure_executions(client):
    client.post("/tickets", json=VALID_REQUEST)
    client.post("/tickets/42/close", json=CLOSE_BODY)          # success
    client.post("/tickets/42/close", json=CLOSE_BODY)          # rejected (409)

    logs = client.get("/execution-logs", params={"limit": 5}).json()
    close_logs = [log for log in logs if log["action"] == "ticket.closed"]
    assert [(log["status"], log["ticket_id"]) for log in close_logs] == [
        ("failure", "42"),
        ("success", "42"),
    ]  # newest first: the 409 attempt logged a failure row
    assert any("invalid transition" in (log["detail"] or "") for log in close_logs)


def test_dashboard_counts_open_and_closed_accurately(client):
    for ticket_id in (42, 43, 44):
        client.post("/tickets", json={**VALID_REQUEST, "id": ticket_id})
    client.post("/tickets/42/close", json=CLOSE_BODY)

    page = client.get("/dashboard").text
    # Cards render as <div class="value">N</div><div class="label">name</div>.
    assert '<div class="value">3</div><div class="label">tickets</div>' in page
    assert '<div class="value">2</div><div class="label">open</div>' in page
    assert '<div class="value">1</div><div class="label">closed</div>' in page


def test_dashboard_counts_include_intake_closed_tickets(client):
    """Tickets closed upstream (completed=true) count as closed too."""
    client.post("/tickets", json=VALID_REQUEST)
    client.post("/tickets", json={**VALID_REQUEST, "id": 45, "completed": True})

    page = client.get("/dashboard").text
    assert '<div class="value">2</div><div class="label">tickets</div>' in page
    assert '<div class="value">1</div><div class="label">open</div>' in page
    assert '<div class="value">1</div><div class="label">closed</div>' in page


def test_legacy_database_gains_closing_columns_without_data_loss(tmp_path):
    """An older flowops.db (no resolution_note/closed_at columns) still works.

    Simulates opening a pre-close database with the new code: the tiny
    SQLite migration adds the missing nullable columns and keeps rows.
    """
    import sqlite3

    from sqlalchemy import text as sa_text

    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE tickets (ticket_id VARCHAR(64) NOT NULL PRIMARY KEY, "
        "requester_id INTEGER NOT NULL, title VARCHAR(500) NOT NULL, "
        "status VARCHAR(16) NOT NULL, priority VARCHAR(16) NOT NULL, "
        "category VARCHAR(32), queue VARCHAR(64), "
        "created_at DATETIME, updated_at DATETIME)"
    )
    connection.execute(
        "INSERT INTO tickets VALUES ('7', 1, 'Old ticket', 'open', 'normal', "
        "NULL, NULL, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
    )
    connection.commit()
    connection.close()

    db.use_database(f"sqlite:///{db_path}")
    client = TestClient(api.app)  # first request triggers create_all + migration

    assert client.get("/tickets/7").json()["resolution_note"] is None
    closed = client.post("/tickets/7/close", json=CLOSE_BODY)
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"

    with db.engine.connect() as connection:
        rows = connection.execute(
            sa_text("SELECT title, resolution_note FROM tickets")
        ).all()
    assert rows == [("Old ticket", CLOSE_BODY["resolution_note"])]
