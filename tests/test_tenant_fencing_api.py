"""Tenant fencing regressions for the core ticket and onboarding APIs."""
import pytest
from fastapi.testclient import TestClient

from src import api, chatops, db
from src.auth import Actor
from src.deps import _actor_from_header

ONBOARDING = {
    "request_type": "onboarding",
    "employee_name": "Jane Doe",
    "employee_email": "jane@example.com",
    "department": "Sales",
    "requester_id": 3,
    "start_date": "2026-11-02",
}


@pytest.fixture
def fencing_client(tmp_path, monkeypatch):
    db.use_database(f"sqlite:///{tmp_path}/tenancy.db")
    db._ensure_tables()
    setup = db.SessionLocal()
    try:
        setup.add(db.TenantRecord(key="tenant-a", name="Tenant A", active=True))
        setup.add(db.TenantRecord(key="tenant-b", name="Tenant B", active=True))
        setup.commit()
    finally:
        setup.close()
    monkeypatch.setenv("FLOWOPS_ENVIRONMENT", "development")
    actors = {"actor": Actor("ops-a", ("automation.operator",), source="bearer", tenant_key="tenant-a")}
    api.app.dependency_overrides[_actor_from_header] = lambda: actors["actor"]
    with TestClient(api.app) as client:
        def set_actor(value):
            actors["actor"] = value
        yield client, set_actor
    api.app.dependency_overrides.pop(_actor_from_header, None)


def _tenant_actor(key):
    return Actor(f"ops-{key}", ("automation.operator",), source="bearer", tenant_key=key)


def _local_actor():
    return Actor("local-development", ("automation.operator", "automation.admin"), source="local")


def test_tenant_actor_sees_only_its_tenant_tickets(fencing_client):
    client, set_actor = fencing_client
    set_actor(_tenant_actor("tenant-a"))
    created = client.post("/tickets", json={
        "id": 1001, "userId": 77, "title": "Email broken for Tenant A",
        "completed": False, "tenant": "tenant-a",
    })
    assert created.status_code == 201, created.text
    ticket_id = created.json()["ticket_id"]

    set_actor(_tenant_actor("tenant-b"))
    assert client.get("/tickets").json() == []
    assert client.get(f"/tickets/{ticket_id}").status_code == 404

    set_actor(_tenant_actor("tenant-a"))
    assert [row["ticket_id"] for row in client.get("/tickets").json()] == [ticket_id]
    assert client.get(f"/tickets/{ticket_id}").status_code == 200


def test_ticket_creation_requires_actor_tenant(fencing_client):
    client, set_actor = fencing_client
    set_actor(_tenant_actor("tenant-b"))
    response = client.post("/tickets", json={
        "id": 2002, "userId": 88, "title": "Cross-tenant write attempt",
        "completed": False, "tenant": "tenant-a",
    })
    assert response.status_code == 404, response.text
    stamped = client.post("/tickets", json={
        "id": 2003, "userId": 88, "title": "No tenant in payload",
        "completed": False,
    })
    assert stamped.status_code == 201, stamped.text
    set_actor(_local_actor())
    stored = {row["ticket_id"] for row in client.get("/tickets").json()}
    assert stamped.json()["ticket_id"] in stored
    session = db.SessionLocal()
    try:
        record = session.get(db.TicketRecord, stamped.json()["ticket_id"])
        assert record.tenant_key == "tenant-b"
    finally:
        session.close()


def test_local_actor_stays_global(fencing_client):
    client, set_actor = fencing_client
    set_actor(_tenant_actor("tenant-a"))
    first = client.post("/tickets", json={"id": 1, "userId": 1, "title": "One", "completed": False})
    set_actor(_tenant_actor("tenant-b"))
    second = client.post("/tickets", json={"id": 2, "userId": 2, "title": "Two", "completed": False})
    assert {first.status_code, second.status_code} == {201}
    set_actor(_local_actor())
    assert len(client.get("/tickets").json()) == 2
    set_actor(_tenant_actor("tenant-a"))
    assert len(client.get("/tickets").json()) == 1


def test_onboarding_requests_are_tenant_scoped(fencing_client):
    client, set_actor = fencing_client
    set_actor(_tenant_actor("tenant-a"))
    created = client.post("/onboarding-requests", json=ONBOARDING)
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]
    assert created.json()["tenant_key"] == "tenant-a"

    set_actor(_tenant_actor("tenant-b"))
    assert client.get("/onboarding-requests").json() == []
    assert client.get(f"/onboarding-requests/{request_id}").status_code == 404
    assert client.get(f"/onboarding-requests/{request_id}/status").status_code == 404
    foreign = client.post("/onboarding-requests", json={**ONBOARDING, "tenant_key": "tenant-a"})
    assert foreign.status_code == 404, foreign.text

    set_actor(_tenant_actor("tenant-a"))
    assert client.get(f"/onboarding-requests/{request_id}").status_code == 200


def test_chatops_token_cannot_leave_its_tenant(fencing_client, monkeypatch):
    client, set_actor = fencing_client
    secret = "chatops-test-secret"
    monkeypatch.setenv("FLOWOPS_CHATOPS_SECRET", secret)
    environ = {"FLOWOPS_CHATOPS_SECRET": secret}
    set_actor(_tenant_actor("tenant-a"))
    created = client.post("/onboarding-requests", json=ONBOARDING)
    assert created.status_code == 201, created.text
    record_id = created.json()["id"]
    item = next(item for item in created.json()["checklist"] if item["requires_approval"])
    token = chatops.generate_token(
        record_id, item["id"], "approve", tenant_key="tenant-b", environ=environ,
    )

    # The action itself is actor-free; the token's tenant binding guards it.
    assert client.post(f"/webhooks/actions/approve?token={token}").status_code == 404
    token = chatops.generate_token(
        record_id, item["id"], "approve", tenant_key="tenant-a", environ=environ,
    )
    approval = client.post(f"/webhooks/actions/approve?token={token}")
    assert approval.status_code == 200, approval.text
    replay = client.post(f"/webhooks/actions/approve?token={token}")
    assert replay.status_code == 409, replay.text


def test_chatops_token_without_tenant_still_actions(fencing_client, monkeypatch):
    """Legacy tokens minted before tenant binding keep working."""
    client, set_actor = fencing_client
    secret = "chatops-test-secret"
    monkeypatch.setenv("FLOWOPS_CHATOPS_SECRET", secret)
    environ = {"FLOWOPS_CHATOPS_SECRET": secret}
    set_actor(_tenant_actor("tenant-a"))
    created = client.post("/onboarding-requests", json=ONBOARDING)
    assert created.status_code == 201, created.text
    record_id = created.json()["id"]
    item = next(item for item in created.json()["checklist"] if item["requires_approval"])
    token = chatops.generate_token(record_id, item["id"], "approve", environ=environ)
    response = client.post(f"/webhooks/actions/approve?token={token}")
    assert response.status_code == 200, response.text
