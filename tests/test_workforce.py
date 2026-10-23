"""Workforce lifecycle, authorization, assets, and automation regressions."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src import api, db, ui_session
from src.auth import Actor
from src.deps import _actor_from_header


@pytest.fixture
def workforce_client(tmp_path, monkeypatch):
    db.use_database(f"sqlite:///{tmp_path}/workforce.db")
    db._ensure_tables()
    setup = db.SessionLocal()
    try:
        setup.add(db.TenantRecord(key="tenant-a", name="Tenant A", active=True))
        setup.add(db.TenantRecord(key="tenant-b", name="Tenant B", active=True))
        setup.commit()
    finally:
        setup.close()
    monkeypatch.setenv("FLOWOPS_ENVIRONMENT", "development")
    monkeypatch.setenv("FLOWOPS_AUTOMATION_CALLBACK_SECRET", "callback-test-secret")
    monkeypatch.setenv("FLOWOPS_N8N_API_KEY", "n8n-test-secret")
    actors = {"actor": Actor("hr-demo", ("HR",), source="session", tenant_key="tenant-a")}
    api.app.dependency_overrides[_actor_from_header] = lambda: actors["actor"]
    with TestClient(api.app) as client:
        def set_actor(value):
            if value.tenant_key is None and value.source == "session":
                value = Actor(value.subject, value.roles, source=value.source, tenant_key="tenant-a")
            actors["actor"] = value
        yield client, set_actor
    api.app.dependency_overrides.pop(_actor_from_header, None)


def _employee(client):
    response = client.post("/workforce/employees", json={
        "employee_number": "EMP-1001", "full_name": "Taylor Nguyen",
        "work_email": "taylor.nguyen@example.test", "department": "Operations",
        "job_title": "Support Analyst", "start_date": (date.today() + timedelta(days=20)).isoformat(),
        "manager_subject": "manager-1",
    })
    assert response.status_code == 201, response.text
    return response.json()


def _approved_request(client, set_actor):
    employee = _employee(client)
    request = client.post("/workforce/onboarding-requests", json={"employee_id": employee["id"]}).json()
    assert client.post(f"/workforce/onboarding-requests/{request['id']}/submit").status_code == 200
    set_actor(Actor("manager-1", ("MANAGER",), source="session"))
    approved = client.post(f"/workforce/onboarding-requests/{request['id']}/approve", json={"comment": "Approved."})
    assert approved.status_code == 200, approved.text
    set_actor(Actor("it-demo", ("IT_ADMIN",), source="session"))
    return employee, request["id"]


def test_manager_approval_creates_tasks_once_and_completes_only_when_required_tasks_done(workforce_client):
    client, set_actor = workforce_client
    employee = _employee(client)
    draft = client.post("/workforce/onboarding-requests", json={"employee_id": employee["id"]}).json()
    request_id = draft["id"]
    assert client.post(f"/workforce/onboarding-requests/{request_id}/submit").status_code == 200

    set_actor(Actor("other-manager", ("MANAGER",), source="session"))
    assert client.post(f"/workforce/onboarding-requests/{request_id}/approve", json={}).status_code == 404
    assert client.get(f"/workforce/onboarding-requests/{request_id}").status_code == 404

    set_actor(Actor("manager-1", ("MANAGER",), source="session"))
    def approve():
        return client.post(f"/workforce/onboarding-requests/{request_id}/approve", json={"comment": "Approved."}).status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        approvals = list(executor.map(lambda _index: approve(), range(2)))
    assert sorted(approvals) == [200, 409]
    detail = client.get(f"/workforce/onboarding-requests/{request_id}").json()
    assert detail["status"] == "IN_PROGRESS"
    assert len(detail["tasks"]) == 5
    assert detail["approval"]["actor_subject"] == "manager-1"
    set_actor(Actor("it-demo", ("IT_ADMIN",), source="session"))
    event_response = client.get("/workforce/automation/events")
    assert event_response.status_code == 200, event_response.text
    event = event_response.json()[0]
    assert client.post("/workforce/automation/callbacks/provisioning", json={"eventId": event["event_id"],
        "tenantKey": "tenant-a", "outcome": "success"}).status_code == 401
    headers = {"X-FlowOps-Automation-Key": "callback-test-secret"}
    payload = {"eventId": event["event_id"], "tenantKey": "tenant-a",
               "outcome": "success", "detail": "simulated"}
    assert client.post("/workforce/automation/callbacks/provisioning", json=payload, headers=headers).json()["duplicate"] is False
    assert client.post("/workforce/automation/callbacks/provisioning", json=payload, headers=headers).json()["duplicate"] is True

    tasks = client.get("/workforce/tasks").json()
    assert sum(task["status"] == "DONE" for task in tasks) == 1
    for task in tasks:
        if task["task_key"] != "provision_account":
            response = client.patch(f"/workforce/tasks/{task['id']}", json={"status": "DONE", "completion_notes": "Checked."})
            assert response.status_code == 200, response.text
    assert client.get(f"/workforce/onboarding-requests/{request_id}").json()["status"] == "COMPLETED"


def test_asset_assignment_is_single_owner_and_return_is_audited(workforce_client):
    client, set_actor = workforce_client
    employee = _employee(client)
    set_actor(Actor("it-demo", ("IT_ADMIN",), source="session"))
    created = client.post("/workforce/assets", json={"asset_tag": "LAP-001", "category": "Laptop", "brand_model": "Example 14"})
    assert created.status_code == 201
    asset_id = created.json()["id"]
    assert client.post(f"/workforce/assets/{asset_id}/assign", json={"employee_id": employee["id"]}).status_code == 200

    set_actor(Actor("hr-demo", ("HR",), source="session"))
    response = client.post("/workforce/employees", json={
        "employee_number": "EMP-1002", "full_name": "Morgan Le",
        "work_email": "morgan.le@example.test", "department": "Operations",
        "job_title": "Support Analyst", "start_date": (date.today() + timedelta(days=20)).isoformat(),
        "manager_subject": "manager-1"})
    assert response.status_code == 201
    second = response.json()
    set_actor(Actor("it-demo", ("IT_ADMIN",), source="session"))
    assert client.post(f"/workforce/assets/{asset_id}/assign", json={"employee_id": second["id"]}).status_code == 409
    assert client.post(f"/workforce/assets/{asset_id}/return", json={"notes": "Returned."}).status_code == 200
    history = client.get(f"/workforce/assets/{asset_id}/history").json()
    assert len(history) == 1 and history[0]["returned_at"] is not None


def test_concurrent_asset_claims_have_exactly_one_winner(workforce_client):
    client, set_actor = workforce_client
    first = _employee(client)
    second_response = client.post("/workforce/employees", json={
        "employee_number": "EMP-1002", "full_name": "Morgan Le", "work_email": "morgan@example.test",
        "department": "Operations", "job_title": "Analyst", "start_date": "2027-02-01",
        "manager_subject": "manager-1"})
    second = second_response.json()
    set_actor(Actor("it-demo", ("IT_ADMIN",), source="session"))
    asset_id = client.post("/workforce/assets", json={"asset_tag": "RACE-001", "category": "Laptop", "brand_model": "Example"}).json()["id"]
    def assign(employee_id):
        return client.post(f"/workforce/assets/{asset_id}/assign", json={"employee_id": employee_id}).status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(assign, [first["id"], second["id"]]))
    assert sorted(statuses) == [200, 409]
    assert client.get("/workforce/assets").json()[0]["status"] == "ASSIGNED"


def test_concurrent_onboarding_creation_has_exactly_one_active_request(workforce_client):
    client, _set_actor = workforce_client
    employee = _employee(client)
    def create_request():
        return client.post("/workforce/onboarding-requests", json={"employee_id": employee["id"]}).status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(lambda _index: create_request(), range(2)))
    assert sorted(statuses) == [201, 409]
    assert len(client.get("/workforce/onboarding-requests").json()) == 1


def test_cancelled_request_rejects_callback_and_worker_skips_queued_event(workforce_client, monkeypatch):
    client, set_actor = workforce_client
    _, request_id = _approved_request(client, set_actor)
    event_response = client.get("/workforce/automation/events")
    assert event_response.status_code == 200, event_response.text
    event = event_response.json()[0]
    set_actor(Actor("hr-demo", ("HR",), source="session"))
    assert client.post(f"/workforce/onboarding-requests/{request_id}/cancel").status_code == 200
    callback = client.post("/workforce/automation/callbacks/provisioning",
        json={"eventId": event["event_id"], "tenantKey": "tenant-a", "outcome": "success"},
        headers={"X-FlowOps-Automation-Key": "callback-test-secret"})
    assert callback.status_code == 409
    monkeypatch.setattr("src.workforce_worker.requests.post", lambda *a, **kw: pytest.fail("cancelled event was sent"))
    from src.workforce_worker import run_once
    assert run_once() is None
    set_actor(Actor("it-demo", ("IT_ADMIN",), source="session"))
    assert client.get("/workforce/automation/events").json()[0]["delivery_status"] == "CANCELLED"


def test_reminder_callback_is_authenticated_and_idempotent(workforce_client):
    client, set_actor = workforce_client
    _, request_id = _approved_request(client, set_actor)
    session = db.SessionLocal()
    try:
        row = session.execute(select(db.ITTaskRecord).where(db.ITTaskRecord.request_id == request_id)).scalars().first()
        row.due_at = db.utcnow() - timedelta(days=1)
        task_id = row.id
        session.commit()
    finally:
        session.close()
    assert client.get("/workforce/integration/overdue-tasks").status_code == 401
    headers = {"X-FlowOps-Automation-Key": "n8n-test-secret"}
    overdue = client.get("/workforce/integration/overdue-tasks?tenant_key=tenant-a", headers=headers)
    assert overdue.status_code == 200 and overdue.json()[0]["taskId"] == task_id
    payload = {"eventId": "reminder:tenant-a:1:2026-10-22", "tenantKey": "tenant-a",
               "taskId": task_id, "outcome": "SIMULATED"}
    assert client.post("/workforce/integration/reminders", json=payload, headers=headers).json()["duplicate"] is False
    assert client.post("/workforce/integration/reminders", json=payload, headers=headers).json()["duplicate"] is True
    error = {"eventId": "execution-99", "workflowName": "daily-reminders", "error": "temporary network failure"}
    assert client.post("/workforce/integration/errors", json=error, headers=headers).json()["duplicate"] is False
    assert client.post("/workforce/integration/errors", json=error, headers=headers).json()["duplicate"] is True


def test_draft_duplicate_email_and_api_role_boundary(workforce_client):
    client, set_actor = workforce_client
    first = _employee(client)
    duplicate = client.post("/workforce/employees", json={
        "employee_number": "EMP-1002", "full_name": "Taylor Nguyen",
        "work_email": first["work_email"], "department": "Operations", "job_title": "Analyst",
        "start_date": "2027-02-01", "manager_subject": "manager-1"})
    assert duplicate.status_code == 409
    set_actor(Actor("manager-1", ("MANAGER",), source="session"))
    assert client.post("/workforce/employees", json={}).status_code == 403


def test_tenant_scope_covers_employees_requests_assets_and_audit(workforce_client):
    client, set_actor = workforce_client
    employee = _employee(client)
    request = client.post("/workforce/onboarding-requests", json={"employee_id": employee["id"]}).json()
    set_actor(Actor("it-a", ("IT_ADMIN",), source="session"))
    asset_response = client.post("/workforce/assets", json={"asset_tag": "TENANT-A-001", "category": "Laptop",
        "brand_model": "Example 14"})
    assert asset_response.status_code == 201, asset_response.text
    asset = asset_response.json()

    set_actor(Actor("hr-b", ("HR",), source="session", tenant_key="tenant-b"))
    assert client.get("/workforce/employees").json() == []
    assert client.get("/workforce/onboarding-requests").json() == []
    assert client.get(f"/workforce/onboarding-requests/{request['id']}").status_code == 404
    assert client.post("/workforce/onboarding-requests", json={"employee_id": employee["id"]}).status_code == 404
    set_actor(Actor("it-b", ("IT_ADMIN",), source="session", tenant_key="tenant-b"))
    assert client.get("/workforce/assets").json() == []
    assert client.get(f"/workforce/assets/{asset['id']}/history").status_code == 404
    assert client.get("/workforce/audit").json() == []


def test_scoped_roles_require_active_tenant_and_duplicate_submission_is_atomic(workforce_client):
    client, set_actor = workforce_client
    employee = _employee(client)
    request = client.post("/workforce/onboarding-requests", json={"employee_id": employee["id"]}).json()
    path = f"/workforce/onboarding-requests/{request['id']}/submit"
    assert client.post(path).status_code == 200
    assert client.post(path).status_code == 409
    set_actor(Actor("unscoped-hr", ("HR",), source="bearer"))
    assert client.get("/workforce/employees").status_code == 403
    set_actor(Actor("inactive-hr", ("HR",), source="session", tenant_key="missing-tenant"))
    assert client.get("/workforce/employees").status_code == 403


def test_local_workforce_access_is_rejected_outside_development(workforce_client, monkeypatch):
    client, set_actor = workforce_client
    set_actor(Actor("local-development", ("automation.admin",), source="local"))
    monkeypatch.setenv("FLOWOPS_ENVIRONMENT", "production")
    assert client.get("/workforce/employees").status_code == 403


def test_employee_termination_cancels_open_work_and_rejects_late_callback(workforce_client):
    client, set_actor = workforce_client
    employee, request_id = _approved_request(client, set_actor)
    event = client.get("/workforce/automation/events").json()[0]
    task_id = client.get("/workforce/tasks").json()[0]["id"]
    headers = {"X-FlowOps-Automation-Key": "callback-test-secret"}
    set_actor(Actor("hr-demo", ("HR",), source="session"))
    assert client.patch(f"/workforce/employees/{employee['id']}/status", json={"status": "TERMINATED"}).status_code == 200
    assert client.get(f"/workforce/onboarding-requests/{request_id}").json()["status"] == "CANCELLED"
    set_actor(Actor("it-demo", ("IT_ADMIN",), source="session"))
    assert client.patch(f"/workforce/tasks/{task_id}", json={"status": "DONE"}).status_code == 409
    assert client.post("/workforce/automation/callbacks/provisioning",
        json={"eventId": event["event_id"], "tenantKey": "tenant-a", "outcome": "success"},
        headers=headers).status_code == 409


def test_worker_lease_token_fences_stale_worker_after_reclaim(workforce_client, monkeypatch):
    client, set_actor = workforce_client
    _, _request_id = _approved_request(client, set_actor)
    event = client.get("/workforce/automation/events").json()[0]
    monkeypatch.setenv("FLOWOPS_AUTOMATION_ADAPTER", "n8n")
    monkeypatch.setenv("FLOWOPS_N8N_WEBHOOK_URL", "https://n8n.example.test/webhook")
    monkeypatch.setenv("FLOWOPS_N8N_WEBHOOK_SECRET", "webhook-secret")

    class Response:
        def raise_for_status(self):
            pass

    from src.workforce_worker import _claim, run_once
    claims = {}
    def reclaim_during_send(*_args, **_kwargs):
        first = db.SessionLocal()
        try:
            row = first.execute(select(db.WorkforceAutomationEventRecord).where(
                db.WorkforceAutomationEventRecord.event_id == event["event_id"])).scalar_one()
            claims["first"] = row.lease_token
            row.lease_until = db.utcnow() - timedelta(seconds=1)
            first.commit()
        finally:
            first.close()
        second = db.SessionLocal()
        try:
            second_claim = _claim(second)
            claims["second"] = second_claim[3]
        finally:
            second.close()
        return Response()

    monkeypatch.setattr("src.workforce_worker.requests.post", reclaim_during_send)
    assert run_once()["status"] == "lease_lost"
    assert claims["first"] != claims["second"]
    session = db.SessionLocal()
    try:
        row = session.execute(select(db.WorkforceAutomationEventRecord).where(
            db.WorkforceAutomationEventRecord.event_id == event["event_id"])).scalar_one()
        assert row.lease_token == claims["second"] and row.delivery_status == "PENDING"
        row.lease_until = db.utcnow() - timedelta(seconds=1)
        session.commit()
    finally:
        session.close()
    monkeypatch.setattr("src.workforce_worker.requests.post", lambda *_args, **_kwargs: Response())
    assert run_once()["status"] == "DELIVERED"


def test_n8n_worker_does_not_dispatch_legacy_event_without_tenant(workforce_client, monkeypatch):
    client, _set_actor = workforce_client
    _employee(client)
    session = db.SessionLocal()
    try:
        employee = session.execute(select(db.EmployeeRecord)).scalar_one()
        employee.tenant_key = None
        request = db.StaffOnboardingRecord(employee_id=employee.id, tenant_key=None,
            status="IN_PROGRESS", created_by="legacy-import")
        session.add(request)
        session.flush()
        event = db.WorkforceAutomationEventRecord(event_id="legacy-no-tenant", event_type="workforce.onboarding.approved",
            request_id=request.id, tenant_key=None,
            payload_json=json.dumps({"eventId": "legacy-no-tenant"}), delivery_status="PENDING")
        session.add(event)
        session.commit()
    finally:
        session.close()
    monkeypatch.setenv("FLOWOPS_AUTOMATION_ADAPTER", "n8n")
    monkeypatch.setattr("src.workforce_worker.requests.post", lambda *_args, **_kwargs: pytest.fail("tenantless legacy event must not be dispatched"))
    from src.workforce_worker import run_once
    result = run_once()
    assert result["status"] == "FAILED"
    session = db.SessionLocal()
    try:
        event = session.execute(select(db.WorkforceAutomationEventRecord).where(
            db.WorkforceAutomationEventRecord.event_id == "legacy-no-tenant")).scalar_one()
        assert event.last_error.startswith("Tenant ownership is unknown")
    finally:
        session.close()


def test_missing_workflow_callback_is_visible_and_retryable_after_timeout(workforce_client):
    client, set_actor = workforce_client
    _employee_record, _request_id = _approved_request(client, set_actor)
    session = db.SessionLocal()
    try:
        event = session.execute(select(db.WorkforceAutomationEventRecord)).scalar_one()
        event.delivery_status = "DELIVERED"
        event.workflow_status = "WAITING"
        event.delivered_at = db.utcnow() - timedelta(minutes=11)
        event.attempts = 1
        event_id = event.event_id
        session.commit()
    finally:
        session.close()
    set_actor(Actor("it-demo", ("IT_ADMIN",), source="session"))
    listed = next(item for item in client.get("/workforce/automation/events").json()
                  if item["event_id"] == event_id)
    assert listed["callback_overdue"] is True
    assert client.post(f"/workforce/automation/events/{event_id}/retry").json()["delivery_status"] == "PENDING"


def test_outbox_retries_temporary_failure_and_retains_exhausted_failure(workforce_client, monkeypatch):
    client, set_actor = workforce_client
    _employee(client)
    session = db.SessionLocal()
    try:
        employee = session.execute(select(db.EmployeeRecord)).scalar_one()
        request = db.StaffOnboardingRecord(employee_id=employee.id, tenant_key=employee.tenant_key,
            status="IN_PROGRESS", created_by="hr-demo")
        session.add(request)
        session.flush()
        event = db.WorkforceAutomationEventRecord(event_id="delivery-retry-1", event_type="workforce.onboarding.approved",
            request_id=request.id, tenant_key=request.tenant_key,
            payload_json=json.dumps({"eventId": "delivery-retry-1"}), delivery_status="PENDING")
        event2 = db.WorkforceAutomationEventRecord(event_id="delivery-fail-1", event_type="workforce.onboarding.approved",
            request_id=request.id, tenant_key=request.tenant_key,
            payload_json=json.dumps({"eventId": "delivery-fail-1"}), delivery_status="PENDING",
            next_attempt_at=db.utcnow() + timedelta(days=1))
        session.add_all([event, event2])
        session.commit()
        event2_id = event2.id
    finally:
        session.close()

    class Response:
        status_code = 200
        def raise_for_status(self): pass

    from requests import Timeout
    calls = {"count": 0}
    def transient_then_ok(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise Timeout("temporary")
        return Response()
    monkeypatch.setenv("FLOWOPS_AUTOMATION_ADAPTER", "n8n")
    monkeypatch.setenv("FLOWOPS_N8N_WEBHOOK_URL", "https://n8n.example.test/webhook/flowops")
    monkeypatch.setenv("FLOWOPS_N8N_WEBHOOK_SECRET", "signing-secret")
    monkeypatch.setenv("FLOWOPS_N8N_API_KEY", "api-secret")
    monkeypatch.setattr("src.workforce_worker.requests.post", transient_then_ok)
    from src.workforce_worker import run_once
    first = run_once()
    assert first["status"] == "PENDING" and first["attempts"] == 1
    session = db.SessionLocal()
    try:
        row = session.execute(select(db.WorkforceAutomationEventRecord).where(
            db.WorkforceAutomationEventRecord.event_id == "delivery-retry-1")).scalar_one()
        row.next_attempt_at = db.utcnow()
        session.commit()
    finally:
        session.close()
    second = run_once()
    assert second["status"] == "DELIVERED" and second["attempts"] == 2
    assert second["workflow_status"] == "WAITING"  # HTTP acceptance is not workflow completion.

    session = db.SessionLocal()
    try:
        row = session.get(db.WorkforceAutomationEventRecord, event2_id)
        row.next_attempt_at = db.utcnow()
        session.commit()
    finally:
        session.close()
    monkeypatch.setattr("src.workforce_worker.requests.post", lambda *a, **kw: (_ for _ in ()).throw(Timeout("offline")))
    for _ in range(5):
        run_once()
        session = db.SessionLocal()
        try:
            row = session.get(db.WorkforceAutomationEventRecord, event2_id)
            if row.delivery_status == "PENDING":
                row.next_attempt_at = db.utcnow()
                session.commit()
        finally:
            session.close()
    session = db.SessionLocal()
    try:
        failed = session.get(db.WorkforceAutomationEventRecord, event2_id)
        assert failed.delivery_status == "FAILED" and failed.attempts == 5
    finally:
        session.close()
    set_actor(Actor("it-demo", ("IT_ADMIN",), source="session"))
    listing = client.get("/workforce/automation/events").json()
    failed_event = next(item for item in listing if item["event_id"] == "delivery-fail-1")
    assert failed_event["delivery_status"] == "FAILED"
    assert client.post("/workforce/automation/events/delivery-fail-1/retry").status_code == 200
    monkeypatch.setattr("src.workforce_worker.requests.post", lambda *a, **kw: Response())
    retried = run_once()
    assert retried["event_id"] == "delivery-fail-1" and retried["status"] == "DELIVERED"
