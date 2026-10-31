"""Tests for readiness, metrics, and request correlation IDs."""

import pytest
from fastapi.testclient import TestClient

from src import api, db


@pytest.fixture
def client(tmp_path):
    db.use_database(f"sqlite:///{tmp_path}/observability.db")
    return TestClient(api.app)


def test_ready_checks_database(client):
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok"}


def test_health_response_carries_correlation_id(client):
    first = client.get("/health")
    assert first.status_code == 200
    assert first.headers.get("X-Correlation-ID")

    second = client.get("/health", headers={"X-Correlation-ID": "test-correlation"})
    assert second.headers["X-Correlation-ID"] == "test-correlation"


def test_metrics_exposes_http_jobs_and_outbox(client):
    client.get("/health")
    payload = {
        "userId": 7,
        "id": 42,
        "title": "Laptop will not start",
        "completed": False,
    }
    client.post("/tickets", json=payload)
    response = client.get("/metrics")
    assert response.status_code == 200
    body = response.text
    assert "flowops_http_requests_total" in body
    assert "flowops_workflow_jobs" in body
    assert "flowops_notification_outbox" in body
    assert "flowops_uptime_seconds" in body
    assert 'route="/tickets"' in body


def test_ready_returns_nonsensitive_503_when_database_connection_fails(client, monkeypatch):
    def fail_connect():
        raise RuntimeError("postgres://user:secret@db.internal/flowops")

    monkeypatch.setattr(db.engine, "connect", fail_connect)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "Database is not ready."}
    assert "secret" not in response.text
