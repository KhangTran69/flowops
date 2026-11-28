"""Tests for the idempotent, admin-only offboarding completion ledger."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src import api, db
from src.routes import onboarding as onboarding_routes

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

    monkeypatch.setattr(onboarding_routes, "offboarding_executor", fake)
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

    monkeypatch.setattr(onboarding_routes, "offboarding_executor", slow)
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
    monkeypatch.setattr(onboarding_routes, "offboarding_executor", lambda key, email: {"action": key})
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

    monkeypatch.setattr(onboarding_routes, "offboarding_executor", failing)
    body = _create_offboarding(client)
    path = f"/offboarding-requests/{body['id']}/steps/recover_laptop/complete"
    with pytest.raises(RuntimeError, match="simulated executor exploded"):
        client.post(path)
    assert _ledger_rows(body["id"]) == []

    monkeypatch.setattr(onboarding_routes, "offboarding_executor", lambda key, email: {"action": key})
    assert client.post(path).status_code == 201
    assert len(calls) == 1


def test_closed_checklist_rejects_completion(client, monkeypatch):
    monkeypatch.setattr(onboarding_routes, "offboarding_executor", lambda key, email: {"action": key})
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


def test_dag_ledger_rejects_out_of_order_completion(client, monkeypatch):
    """offboarding-dag: the ledger enforces prerequisite order like onboarding.

    ``revoke_licenses`` depends on ``disable_user_account``; completing it
    first must return 409 naming the blocker, leave no ledger row behind,
    and succeed once the prerequisite is done.
    """
    monkeypatch.setattr(
        onboarding_routes, "offboarding_executor", lambda key, email: {"action": key}
    )
    body = client.post(
        "/onboarding-requests", json={**OFFBOARDING, "template": "offboarding-dag"}
    ).json()
    checklist_id = body["id"]
    assert _step(body, "revoke_licenses")["status"] == "blocked"

    response = client.post(
        f"/offboarding-requests/{checklist_id}/steps/revoke_licenses/complete"
    )
    assert response.status_code == 409
    assert "disable_user_account" in response.json()["detail"]
    # The rejected attempt must not poison the (checklist, step) ledger key.
    assert _ledger_rows(checklist_id) == []

    gated = _step(body, "disable_user_account")
    assert client.post(
        f"/onboarding-requests/{checklist_id}/items/{gated['id']}/approve"
    ).status_code == 200
    assert client.post(
        f"/offboarding-requests/{checklist_id}/steps/disable_user_account/complete"
    ).status_code == 201
    assert client.post(
        f"/offboarding-requests/{checklist_id}/steps/revoke_licenses/complete"
    ).status_code == 201


def test_dag_ledger_rejects_skipped_step(client):
    """offboarding-dag: a step skipped by its predicate cannot be completed.

    The skipped state is written directly to keep the test focused on the
    ledger's guard (predicate evaluation itself is covered by the v1.3
    tests); the endpoint must still refuse with a clear reason and leave
    no claim behind.
    """
    body = client.post(
        "/onboarding-requests", json={**OFFBOARDING, "template": "offboarding-dag"}
    ).json()
    checklist_id = body["id"]
    # recover_laptop has no prerequisites and no approval gate, so reaching
    # the endpoint's skipped-guard is not shadowed by an earlier rejection.
    skipped = _step(body, "recover_laptop")

    session = db.SessionLocal()
    try:
        item = session.get(db.ChecklistItemRecord, skipped["id"])
        item.status = "skipped"
        session.commit()
    finally:
        session.close()

    response = client.post(
        f"/offboarding-requests/{checklist_id}/steps/recover_laptop/complete"
    )
    assert response.status_code == 409
    assert "skipped" in response.json()["detail"].lower()
    assert _ledger_rows(checklist_id) == []


def test_offboarding_definition_honors_any_join_and_root_predicate(client, monkeypatch):
    calls = []

    def fake(step_key, email):
        calls.append(step_key)
        return {"action": step_key}

    monkeypatch.setattr(onboarding_routes, "offboarding_executor", fake)
    key = "offboarding-conditional-join"
    steps = [
        {"key": "recover_laptop", "step": "Recover laptop", "queue": "it-hardware"},
        {"key": "disable_user_account", "step": "Disable account", "queue": "it-access-management", "requires_approval": True},
        {"key": "revoke_licenses", "step": "Revoke licenses", "queue": "it-software",
         "depends_on": ["recover_laptop", "disable_user_account"], "join": "any"},
        {"key": "archive_mailbox", "step": "Archive mailbox", "queue": "it-messaging",
         "when": ["output.user_id is not null"]},
    ]
    definition = client.post(
        "/workflow-definitions",
        json={"key": key, "name": "Conditional offboarding", "request_type": "offboarding", "steps": steps},
    )
    assert definition.status_code == 200, definition.text
    assert client.post(f"/workflow-definitions/{key}/publish", json={"version": 1}).status_code == 200
    assert client.put(f"/workflow-definitions/{key}", json={"steps": steps}).status_code == 200
    draft_request = client.post(
        "/onboarding-requests", json={**OFFBOARDING, "definition_key": key, "definition_version": 2},
    )
    assert draft_request.status_code == 409
    created = client.post("/onboarding-requests", json={**OFFBOARDING, "definition_key": key})
    assert created.status_code == 201, created.text
    body = created.json()
    statuses = {item["step_key"]: item["status"] for item in body["checklist"]}
    assert statuses["archive_mailbox"] == "skipped"
    assert statuses["revoke_licenses"] == "blocked"
    prefix = f"/offboarding-requests/{body['id']}/steps"
    assert client.post(f"{prefix}/archive_mailbox/complete").status_code == 409
    assert client.post(f"{prefix}/revoke_licenses/complete").status_code == 409
    assert calls == []
    assert client.post(f"{prefix}/recover_laptop/complete").status_code == 201
    current = client.get(f"/onboarding-requests/{body['id']}").json()
    statuses = {item["step_key"]: item["status"] for item in current["checklist"]}
    assert statuses["disable_user_account"] == "pending"
    assert statuses["revoke_licenses"] == "pending"
    assert client.post(f"{prefix}/revoke_licenses/complete").status_code == 201
    assert calls == ["recover_laptop", "revoke_licenses"]


def test_offboarding_migration_reevaluates_changed_pending_predicate(client, monkeypatch):
    def unexpected_execution(key, email):
        pytest.fail("A newly skipped step must not call the executor")

    monkeypatch.setattr(onboarding_routes, "offboarding_executor", unexpected_execution)
    key = "offboarding-new-predicate"
    steps = [{"key": "recover_laptop", "step": "Recover laptop", "queue": "it-hardware"}]
    definition = client.post(
        "/workflow-definitions",
        json={"key": key, "name": "Predicate migration", "request_type": "offboarding", "steps": steps},
    )
    assert definition.status_code == 200, definition.text
    assert client.post(f"/workflow-definitions/{key}/publish", json={"version": 1}).status_code == 200
    created = client.post("/onboarding-requests", json={**OFFBOARDING, "definition_key": key})
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]
    assert created.json()["checklist"][0]["status"] == "pending"
    changed = [{**steps[0], "when": ["output.user_id is not null"]}]
    assert client.put(f"/workflow-definitions/{key}", json={"steps": changed}).status_code == 200
    assert client.post(f"/workflow-definitions/{key}/publish", json={"version": 2}).status_code == 200
    migrated = client.post(f"/onboarding-requests/{request_id}/definition", json={"version": 2, "dry_run": False})
    assert migrated.status_code == 200, migrated.text
    assert client.get(f"/onboarding-requests/{request_id}").json()["checklist"][0]["status"] == "skipped"
    assert client.post(f"/offboarding-requests/{request_id}/steps/recover_laptop/complete").status_code == 409
    assert _ledger_rows(request_id) == []
