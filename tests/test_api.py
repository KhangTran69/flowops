"""Tests for the FlowOps API: intake, routing, idempotency, webhooks, logs.

Fully offline: SQLite in a temp directory, HTTP via TestClient.
"""

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

    def fake_deliver(url, payload):
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


# --- Dashboard (v1.0 preview) ---


def test_dashboard_renders_stats(client):
    client.post("/tickets", json=VALID_REQUEST)
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "FlowOps monitoring dashboard" in response.text
    assert "ticket.created" in response.text
