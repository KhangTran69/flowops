"""PostgreSQL workforce races: disposable database only."""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src import api, db
from src.auth import Actor
from src.deps import _actor_from_header

POSTGRES_URL = os.environ.get("FLOWOPS_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="FLOWOPS_TEST_POSTGRES_URL is not set")


@pytest.fixture
def workforce(monkeypatch):
    db.use_database(POSTGRES_URL)
    db.Base.metadata.drop_all(bind=db.engine)
    db.Base.metadata.create_all(bind=db.engine)
    monkeypatch.setenv("FLOWOPS_ENVIRONMENT", "development")
    monkeypatch.setenv("FLOWOPS_AUTOMATION_ADAPTER", "simulated")
    monkeypatch.setenv("FLOWOPS_AUTOMATION_CALLBACK_SECRET", "synthetic-test-key")
    with db.SessionLocal() as session:
        session.add(db.TenantRecord(key="tenant-a", name="Tenant A", active=True))
        session.commit()
    actor = Actor("manager-a", ("automation.admin",), source="bearer", tenant_key="tenant-a")
    api.app.dependency_overrides[_actor_from_header] = lambda: actor
    try:
        with TestClient(api.app) as client:
            employee = client.post("/workforce/employees", json={
                "employee_number": "PG-01", "full_name": "Synthetic employee",
                "work_email": "pg@example.test", "department": "IT", "job_title": "Analyst",
                "start_date": "2027-01-01", "manager_subject": "manager-a",
            })
            assert employee.status_code == 201, employee.text
            draft = client.post("/workforce/onboarding-requests", json={"employee_id": employee.json()["id"]})
            assert draft.status_code == 201, draft.text
            request_id = draft.json()["id"]
            assert client.post(f"/workforce/onboarding-requests/{request_id}/submit").status_code == 200
            assert client.post(f"/workforce/onboarding-requests/{request_id}/approve", json={}).status_code == 200
            yield client, request_id
    finally:
        api.app.dependency_overrides.pop(_actor_from_header, None)
        db.Base.metadata.drop_all(bind=db.engine)


def test_postgres_callback_and_cancel_serialize_without_deadlock(workforce):
    client, request_id = workforce
    event = client.get("/workforce/automation/events").json()[0]
    gate = Barrier(2)

    def cancel():
        gate.wait(timeout=10)
        return client.post(f"/workforce/onboarding-requests/{request_id}/cancel").status_code

    def callback():
        gate.wait(timeout=10)
        return client.post("/workforce/automation/callbacks/provisioning", headers={
            "X-FlowOps-Automation-Key": "synthetic-test-key",
        }, json={"eventId": event["event_id"], "tenantKey": "tenant-a", "outcome": "success"}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancelled, completed = pool.submit(cancel), pool.submit(callback)
        assert cancelled.result(timeout=20) == 200
        assert completed.result(timeout=20) in (200, 409)
    assert client.get(f"/workforce/onboarding-requests/{request_id}").json()["status"] == "CANCELLED"
    with db.SessionLocal() as session:
        row = session.execute(select(db.WorkforceAutomationEventRecord)).scalar_one()
        assert row.delivery_status == "CANCELLED"
        assert row.workflow_status in {"CANCELLED", "SUCCEEDED"}
        assert row.lease_token is None


def test_postgres_duplicate_task_completion_preserves_original_notes(workforce):
    client, request_id = workforce
    task = next(row for row in client.get(f"/workforce/onboarding-requests/{request_id}").json()["tasks"]
                if row["task_key"] == "prepare_laptop")
    gate = Barrier(2)

    def complete(note):
        gate.wait(timeout=10)
        response = client.patch(f"/workforce/tasks/{task['id']}", json={"status": "DONE", "completion_notes": note})
        return response.status_code, note

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(complete, ["first", "second"]))
    assert sorted(status for status, _ in results) == [200, 409]
    winner = next(note for status, note in results if status == 200)
    tasks = client.get(f"/workforce/onboarding-requests/{request_id}").json()["tasks"]
    assert next(row for row in tasks if row["id"] == task["id"])["completion_notes"] == winner


def test_postgres_manager_audit_filters_text_entity_ids(workforce):
    client, request_id = workforce
    actor = Actor("manager-a", ("MANAGER",), source="bearer", tenant_key="tenant-a")
    api.app.dependency_overrides[_actor_from_header] = lambda: actor
    response = client.get("/workforce/audit")
    assert response.status_code == 200, response.text
    assert any(row["entity_id"] == str(request_id) and row["action"] == "onboarding.approved"
               for row in response.json())
    other = Actor("other-manager", ("MANAGER",), source="bearer", tenant_key="tenant-a")
    api.app.dependency_overrides[_actor_from_header] = lambda: other
    assert client.get("/workforce/audit").json() == []
