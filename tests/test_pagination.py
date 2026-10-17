"""Tests for API/UI pagination and filter boundaries."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src import api, db

TICKET = {
    "userId": 7,
    "id": 1,
    "title": "Laptop will not start",
    "completed": False,
}

ONBOARDING = {
    "request_type": "onboarding",
    "employee_name": "Jane Doe",
    "employee_email": "jane@example.com",
    "department": "Sales",
    "requester_id": 3,
}


@pytest.fixture
def client(tmp_path):
    db.use_database(f"sqlite:///{tmp_path}/pagination.db")
    return TestClient(api.app)


def test_ticket_pagination_keeps_numeric_order(client):
    for ticket_id in range(1, 7):
        assert client.post("/tickets", json={**TICKET, "id": ticket_id}).status_code == 201
    page = client.get("/tickets", params={"limit": 2, "offset": 2}).json()
    assert [ticket["ticket_id"] for ticket in page] == ["3", "4"]
    assert client.get("/tickets", params={"limit": 100, "offset": 99}).json() == []


def test_execution_log_pagination_and_status_filter(client):
    for ticket_id in range(1, 4):
        client.post("/tickets", json={**TICKET, "id": ticket_id})
    full = client.get("/execution-logs", params={"limit": 50}).json()
    page = client.get("/execution-logs", params={"limit": 1, "offset": 1}).json()
    assert len(page) == 1
    assert page[0]["id"] == full[1]["id"]
    assert client.get("/execution-logs", params={"status": "failure"}).json() == []


def test_workflow_job_pagination(client):
    created = client.post("/onboarding-requests", json=ONBOARDING).json()
    items = [item for item in created["checklist"] if not item["requires_approval"]]
    for item in items[:2]:
        client.post(f"/onboarding-requests/{created['id']}/items/{item['id']}/jobs")
    page = client.get("/workflow-jobs", params={"limit": 1, "offset": 1}).json()
    assert len(page) == 1
    assert client.get("/workflow-jobs", params={"limit": 1, "offset": 99}).json() == []
    assert client.get("/ui/workflow-jobs", params={"page": 2}).status_code == 200


def test_notification_pagination(client):
    db.Base.metadata.create_all(bind=db.engine)
    session = db.SessionLocal()
    try:
        for index in range(3):
            session.add(
                db.NotificationOutboxRecord(
                    url=f"https://hooks.example/{index}",
                    payload_json="{}",
                    status="pending",
                    attempts=0,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
        session.commit()
    finally:
        session.close()
    page = client.get("/notifications/pending", params={"limit": 1, "offset": 1}).json()
    assert len(page) == 1
    assert client.get("/notifications/pending", params={"limit": 1, "offset": 99}).json() == []
    assert client.get("/ui/notifications", params={"page": 2}).status_code == 200


def test_audit_pagination_and_actor_filter(client, monkeypatch):
    monkeypatch.setenv("FLOWOPS_AUTH_REQUIRED", "true")
    monkeypatch.setenv("FLOWOPS_DEV_BEARER_TOKEN", "dev-secret")
    headers = {"Authorization": "Bearer dev-secret"}
    for ticket_id in range(1, 3):
        client.post("/tickets", json={**TICKET, "id": ticket_id}, headers=headers)
    full = client.get("/audit", params={"limit": 50}).json()
    page = client.get("/audit", params={"limit": 1, "offset": 1}).json()
    assert page[0]["id"] == full[1]["id"]
    filtered = client.get(
        "/audit", params={"actor": "configured-bearer", "status": "success"}
    ).json()
    assert filtered and all(row["actor_subject"] == "configured-bearer" for row in filtered)
    assert client.get("/ui/audit", params={"page": 2}).status_code == 200
