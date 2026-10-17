"""Tests for the idempotent, admin-only offboarding completion ledger."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src import api, db

OFFBOARDING = {
    "request_type": "offboarding",
    "employee_name": "Jane Doe",
    "employee_email": "jane@example.com",
    "department": "Sales",
    "requester_id": 3,
}


@pytest.fixture
def client(tmp_path):
    db.use_database(f"sqlite:///{tmp_path}/offboarding.db")
    return TestClient(api.app)


def _create_offboarding(client):
    response = client.post("/onboarding-requests", json=OFFBOARDING)
    assert response.status_code == 201
    return response.json()


def _step(body, key):
    return next(item for item in body["checklist"] if item["step_key"] == key)


def _ledger_rows(checklist_id):
    session = db.SessionLocal()
    try:
        return session.execute(
            select(db.OffboardingCompletionRecord).where(
                db.OffboardingCompletionRecord.checklist_id == checklist_id
            )
        ).scalars().all()
    finally:
        session.close()


def test_first_completion_executes_once_and_replays(client, monkeypatch):
    calls = []

    def fake(step_key, email):
        calls.append((step_key, email))
        return {"simulated": True, "action": step_key, "target": email}

    monkeypatch.setattr(api, "offboarding_executor", fake)
    body = _create_offboarding(client)
    path = f"/offboarding-requests/{body['id']}/steps/recover_laptop/complete"

    first = client.post(path)
    assert first.status_code == 201
    assert first.json()["step"]["executor_called"] is True
    assert len(calls) == 1

    second = client.post(path)
    assert second.status_code == 200
    assert second.headers["X-Offboarding-Replayed"] == "true"
    assert second.json() == first.json()
    assert len(calls) == 1
    rows = _ledger_rows(body["id"])
    assert len(rows) == 1
    assert rows[0].status == "completed"


def test_concurrent_requests_claim_before_executing(client, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def slow(step_key, email):
        calls.append((step_key, email))
        entered.set()
        assert release.wait(timeout=5)
        return {"simulated": True, "action": step_key, "target": email}

    monkeypatch.setattr(api, "offboarding_executor", slow)
    body = _create_offboarding(client)
    path = f"/offboarding-requests/{body['id']}/steps/recover_laptop/complete"

    with ThreadPoolExecutor(max_workers=1) as executor:
        first_future = executor.submit(client.post, path)
        assert entered.wait(timeout=5), "first request never reached the executor"
        second = client.post(path)
        release.set()
        first = first_future.result(timeout=5)

    assert first.status_code == 201
    assert second.status_code == 409
    assert "already running" in second.json()["detail"]
    assert len(calls) == 1
    replay = client.post(path)
    assert replay.status_code == 200
    assert replay.headers["X-Offboarding-Replayed"] == "true"
    assert len(calls) == 1


def test_validation_errors_and_approval_gate(client, monkeypatch):
    monkeypatch.setattr(api, "offboarding_executor", lambda key, email: {"action": key})
    body = _create_offboarding(client)
    checklist_id = body["id"]
    assert client.post(
        f"/offboarding-requests/{checklist_id}/steps/not_a_real_step/complete"
    ).status_code == 422
    assert client.post(
        "/offboarding-requests/999/steps/recover_laptop/complete"
    ).status_code == 404

    onboarding = client.post(
        "/onboarding-requests",
        json={**OFFBOARDING, "request_type": "onboarding", "employee_name": "New Hire"},
    ).json()
    assert client.post(
        f"/offboarding-requests/{onboarding['id']}/steps/provision_laptop/complete"
    ).status_code == 409

    gated = _step(body, "disable_user_account")
    assert client.post(
        f"/offboarding-requests/{checklist_id}/steps/disable_user_account/complete"
    ).status_code == 409
    approved = client.post(
        f"/onboarding-requests/{checklist_id}/items/{gated['id']}/approve"
    )
    assert approved.status_code == 200
    assert client.post(
        f"/offboarding-requests/{checklist_id}/steps/disable_user_account/complete"
    ).status_code == 201


def test_executor_failure_removes_claim_and_allows_retry(client, monkeypatch):
    calls = []

    def failing(step_key, email):
        calls.append(step_key)
        raise RuntimeError("simulated executor exploded")

    monkeypatch.setattr(api, "offboarding_executor", failing)
    body = _create_offboarding(client)
    path = f"/offboarding-requests/{body['id']}/steps/recover_laptop/complete"
    with pytest.raises(RuntimeError, match="simulated executor exploded"):
        client.post(path)
    assert _ledger_rows(body["id"]) == []

    monkeypatch.setattr(api, "offboarding_executor", lambda key, email: {"action": key})
    assert client.post(path).status_code == 201
    assert len(calls) == 1


def test_closed_checklist_rejects_completion(client, monkeypatch):
    monkeypatch.setattr(api, "offboarding_executor", lambda key, email: {"action": key})
    body = _create_offboarding(client)
    session = db.SessionLocal()
    try:
        record = session.get(db.OnboardingRequestRecord, body["id"])
        record.status = "closed"
        session.commit()
    finally:
        session.close()
    assert client.post(
        f"/offboarding-requests/{body['id']}/steps/recover_laptop/complete"
    ).status_code == 409


def test_offboarding_completion_requires_admin(client, monkeypatch):
    body = _create_offboarding(client)
    monkeypatch.setenv("FLOWOPS_AUTH_REQUIRED", "true")
    monkeypatch.setenv("FLOWOPS_DEV_BEARER_TOKEN", "dev-secret")
    path = f"/offboarding-requests/{body['id']}/steps/recover_laptop/complete"
    assert client.post(path).status_code == 401
    operator = client.post(path, headers={"Authorization": "Bearer dev-secret"})
    assert operator.status_code == 403
