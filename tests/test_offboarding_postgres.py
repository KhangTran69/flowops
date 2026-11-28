"""PostgreSQL coverage for the offboarding completion ledger."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src import api, db
from src.routes import onboarding as onboarding_routes

POSTGRES_URL = os.environ.get("FLOWOPS_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL, reason="FLOWOPS_TEST_POSTGRES_URL is not set"
)

OFFBOARDING = {
    "request_type": "offboarding",
    "employee_name": "Jane Doe",
    "employee_email": "jane@example.com",
    "department": "Sales",
    "requester_id": 3,
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


def test_postgres_offboarding_claim_and_replay(client, monkeypatch):
    calls = []

    def fake(step_key, email):
        calls.append(step_key)
        return {"simulated": True, "action": step_key, "target": email}

    monkeypatch.setattr(onboarding_routes, "offboarding_executor", fake)
    created = client.post("/onboarding-requests", json=OFFBOARDING)
    assert created.status_code == 201
    checklist_id = created.json()["id"]
    path = f"/offboarding-requests/{checklist_id}/steps/recover_laptop/complete"

    first = client.post(path)
    assert first.status_code == 201
    replay = client.post(path)
    assert replay.status_code == 200
    assert replay.headers["X-Offboarding-Replayed"] == "true"
    assert len(calls) == 1

    session = db.SessionLocal()
    try:
        rows = session.execute(
            select(db.OffboardingCompletionRecord).where(
                db.OffboardingCompletionRecord.checklist_id == checklist_id
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "completed"
    finally:
        session.close()


def _ledger_states(checklist_id):
    with db.SessionLocal() as session:
        rows = session.execute(
            select(db.OffboardingCompletionRecord).where(
                db.OffboardingCompletionRecord.checklist_id == checklist_id
            )
        ).scalars().all()
        return {row.step_key: row.status for row in rows}


def test_postgres_dag_ledger_enforces_prerequisites(client, monkeypatch):
    calls = []

    def fake(key, email):
        calls.append(key)
        return {"action": key}

    monkeypatch.setattr(onboarding_routes, "offboarding_executor", fake)
    created = client.post(
        "/onboarding-requests", json={**OFFBOARDING, "template": "offboarding-dag"}
    )
    assert created.status_code == 201, created.text
    body = created.json()
    checklist_id = body["id"]
    prefix = f"/offboarding-requests/{checklist_id}/steps"
    blocked = client.post(f"{prefix}/revoke_licenses/complete")
    assert blocked.status_code == 409
    assert "disable_user_account" in blocked.json()["detail"]
    assert _ledger_states(checklist_id) == {}
    assert calls == []

    gated = next(item for item in body["checklist"] if item["step_key"] == "disable_user_account")
    approved = client.post(f"/onboarding-requests/{checklist_id}/items/{gated['id']}/approve")
    assert approved.status_code == 200, approved.text
    assert client.post(f"{prefix}/disable_user_account/complete").status_code == 201
    completed = client.post(f"{prefix}/revoke_licenses/complete")
    assert completed.status_code == 201, completed.text
    replay = client.post(f"{prefix}/revoke_licenses/complete")
    assert replay.status_code == 200
    assert replay.headers["X-Offboarding-Replayed"] == "true"
    assert replay.json() == completed.json()
    assert calls == ["disable_user_account", "revoke_licenses"]
    assert _ledger_states(checklist_id) == {
        "disable_user_account": "completed", "revoke_licenses": "completed"
    }


def test_postgres_dag_ledger_rejects_skipped_step(client, monkeypatch):
    def unexpected_execution(key, email):
        pytest.fail("A skipped step must not call the executor")

    monkeypatch.setattr(onboarding_routes, "offboarding_executor", unexpected_execution)
    created = client.post(
        "/onboarding-requests", json={**OFFBOARDING, "template": "offboarding-dag"}
    )
    assert created.status_code == 201, created.text
    body = created.json()
    skipped = next(item for item in body["checklist"] if item["step_key"] == "recover_laptop")
    with db.SessionLocal() as session:
        item = session.get(db.ChecklistItemRecord, skipped["id"])
        item.status = "skipped"
        session.commit()

    rejected = client.post(f"/offboarding-requests/{body['id']}/steps/recover_laptop/complete")
    assert rejected.status_code == 409
    assert "skipped" in rejected.json()["detail"].lower()
    assert _ledger_states(body["id"]) == {}


def test_postgres_ledger_replays_after_definition_migration(client, monkeypatch):
    calls = []

    def fake(key, email):
        calls.append(key)
        return {"action": key}

    monkeypatch.setattr(onboarding_routes, "offboarding_executor", fake)
    key = "pg-offboarding-replay"
    steps = [
        {"key": "recover_laptop", "step": "Recover laptop", "queue": "it-hardware"},
        {"key": "archive_mailbox", "step": "Archive mailbox", "queue": "it-messaging",
         "depends_on": ["recover_laptop"]},
    ]
    definition = client.post(
        "/workflow-definitions",
        json={"key": key, "name": "Offboarding replay", "request_type": "offboarding", "steps": steps},
    )
    assert definition.status_code == 200, definition.text
    assert client.post(f"/workflow-definitions/{key}/publish", json={"version": 1}).status_code == 200
    created = client.post("/onboarding-requests", json={**OFFBOARDING, "definition_key": key})
    assert created.status_code == 201, created.text
    checklist_id = created.json()["id"]
    prefix = f"/offboarding-requests/{checklist_id}/steps"
    original = client.post(f"{prefix}/recover_laptop/complete")
    assert original.status_code == 201, original.text

    changed = [dict(step) for step in steps]
    changed[0]["step"] = "Recover assigned laptop"
    version = client.put(f"/workflow-definitions/{key}", json={"steps": changed})
    assert version.status_code == 200, version.text
    assert client.post(f"/workflow-definitions/{key}/publish", json={"version": 2}).status_code == 200
    migrated = client.post(
        f"/onboarding-requests/{checklist_id}/definition", json={"version": 2, "dry_run": False}
    )
    assert migrated.status_code == 200, migrated.text
    current = client.get(f"/onboarding-requests/{checklist_id}").json()
    assert current["definition_version"] == 2
    retained = next(item for item in current["checklist"] if item["step_key"] == "recover_laptop")
    assert retained["status"] == "completed"
    assert retained["step"] == "Recover assigned laptop"

    replay = client.post(f"{prefix}/recover_laptop/complete")
    assert replay.status_code == 200
    assert replay.headers["X-Offboarding-Replayed"] == "true"
    assert replay.json() == original.json()
    assert calls == ["recover_laptop"]
    assert _ledger_states(checklist_id) == {"recover_laptop": "completed"}
    assert client.post(f"{prefix}/archive_mailbox/complete").status_code == 201
    assert calls == ["recover_laptop", "archive_mailbox"]


def test_postgres_definition_migration_cannot_race_offboarding_claim(client, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def fake(key, email):
        calls.append(key)
        entered.set()
        assert release.wait(timeout=10), "test never released the executor"
        return {"action": key}

    monkeypatch.setattr(onboarding_routes, "offboarding_executor", fake)
    key = "pg-offboarding-migration-race"
    steps = [
        {"key": "recover_laptop", "step": "Recover laptop", "queue": "it-hardware"},
        {"key": "archive_mailbox", "step": "Archive mailbox", "queue": "it-messaging"},
    ]
    created = client.post(
        "/workflow-definitions",
        json={"key": key, "name": "Migration race", "request_type": "offboarding", "steps": steps},
    )
    assert created.status_code == 200, created.text
    assert client.post(f"/workflow-definitions/{key}/publish", json={"version": 1}).status_code == 200
    request = client.post("/onboarding-requests", json={**OFFBOARDING, "definition_key": key})
    assert request.status_code == 201, request.text
    request_id = request.json()["id"]
    completion_path = f"/offboarding-requests/{request_id}/steps/recover_laptop/complete"
    migration_path = f"/onboarding-requests/{request_id}/definition"
    changed = [dict(step) for step in steps]
    changed[0]["step"] = "Recover assigned laptop"
    assert client.put(f"/workflow-definitions/{key}", json={"steps": changed}).status_code == 200
    rejected = client.post(migration_path, json={"version": 2, "dry_run": False})
    assert rejected.status_code == 409
    assert "unpublished" in rejected.json()["detail"]
    assert client.post(f"/workflow-definitions/{key}/publish", json={"version": 2}).status_code == 200

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(client.post, completion_path)
        try:
            assert entered.wait(timeout=5), "executor was not reached"
            rejected = client.post(migration_path, json={"version": 2, "dry_run": False})
            assert rejected.status_code == 409
            assert "active ledger claim" in rejected.json()["detail"]
            assert _ledger_states(request_id) == {"recover_laptop": "running"}
            duplicate = client.post(completion_path)
            assert duplicate.status_code == 409
        finally:
            release.set()
        completed = pending.result(timeout=5)
    assert completed.status_code == 201, completed.text
    assert calls == ["recover_laptop"]
    applied = client.post(migration_path, json={"version": 2, "dry_run": False})
    assert applied.status_code == 200, applied.text
    replay = client.post(completion_path)
    assert replay.status_code == 200
    assert replay.json() == completed.json()
    assert calls == ["recover_laptop"]

    changed[0]["depends_on"] = ["archive_mailbox"]
    assert client.put(f"/workflow-definitions/{key}", json={"steps": changed}).status_code == 200
    assert client.post(f"/workflow-definitions/{key}/publish", json={"version": 3}).status_code == 200
    rejected = client.post(migration_path, json={"version": 3, "dry_run": False})
    assert rejected.status_code == 409
    assert "execution rules" in rejected.json()["detail"]
    assert client.get(f"/onboarding-requests/{request_id}").json()["definition_version"] == 2


def test_postgres_offboarding_definition_honors_any_join_and_root_predicate(client, monkeypatch):
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
