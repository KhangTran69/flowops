"""Offline tests for v1.3 definitions, joins, predicates, timers, parity."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src import db, definitions, onboarding, timers
from src.onboarding import StepState, resolve_blocked_steps

STEPS = [
    {
        "key": "create_user_account",
        "step": "Create user account",
        "queue": "it-access-management",
        "requires_approval": True,
        "action": "none",
        "depends_on": [],
        "join": "all",
        "when": [],
    },
    {
        "key": "configure_mailbox",
        "step": "Configure mailbox",
        "queue": "it-messaging",
        "requires_approval": False,
        "action": "none",
        "depends_on": ["create_user_account"],
        "join": "all",
        "when": ["output.user_id is not null"],
        "sla_minutes": 1,
        "escalation_queue": "it-messaging-escalations",
    },
    {
        "key": "independent",
        "step": "Independent hardware",
        "queue": "it-hardware",
        "requires_approval": False,
        "action": "none",
        "depends_on": [],
        "join": "all",
        "when": [],
        "sla_minutes": 1,
        "escalation_queue": "it-hardware-escalations",
    },
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWOPS_AUTH_REQUIRED", "false")
    monkeypatch.setenv("FLOWOPS_UI_AUTH_REQUIRED", "false")
    monkeypatch.setenv("FLOWOPS_RATE_LIMIT_PER_MINUTE", "0")
    db.use_database(f"sqlite:///{tmp_path}/v13.db")
    with TestClient(api_app()) as test_client:
        yield test_client


def api_app():
    from src import api

    return api.app


def _definition(client, steps=STEPS, key="custom-onboarding"):
    created = client.post(
        "/workflow-definitions",
        json={
            "key": key,
            "name": "Custom onboarding",
            "request_type": "onboarding",
            "steps": steps,
        },
    )
    assert created.status_code == 200, created.text
    published = client.post(
        f"/workflow-definitions/{key}/publish", json={"version": 1}
    )
    assert published.status_code == 200, published.text
    return key


def _request(client, key):
    response = client.post(
        "/onboarding-requests",
        json={
            "request_type": "onboarding",
            "employee_name": "Dana",
            "employee_email": "dana@example.com",
            "department": "Engineering",
            "requester_id": 1,
            "definition_key": key,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_definition_versions_and_request_pinning(client):
    key = _definition(client)
    body = _request(client, key)
    assert body["definition_key"] == key
    assert body["definition_version"] == 1

    changed = [dict(step) for step in STEPS]
    changed[1]["step"] = "Configure mailbox v2"
    updated = client.put(f"/workflow-definitions/{key}", json={"steps": changed})
    assert updated.status_code == 200
    detail = client.get(f"/workflow-definitions/{key}").json()
    assert detail["current_version"] == 1  # draft v2 is not published
    # The in-flight request is still pinned to v1.
    current = client.get(f"/onboarding-requests/{body['id']}").json()
    assert current["definition_version"] == 1
    assert any(step["step"] == "Configure mailbox" for step in current["checklist"])

    client.post(f"/workflow-definitions/{key}/publish", json={"version": 2})
    assert client.get(f"/workflow-definitions/{key}").json()["current_version"] == 2


def test_any_join_and_predicate_skip():
    states = {
        "a": StepState("a", "completed"),
        "b": StepState("b", "failed"),
        "any_step": StepState("any_step", "blocked", depends_on=("a", "b"), join_mode="any"),
        "skipped_step": StepState(
            "skipped_step", "blocked", when=("output.user_id is not null",)
        ),
    }
    transitions = resolve_blocked_steps(
        states, scope={"output": {"user_id": None}}
    )
    by_key = {row.key: row.to_status for row in transitions}
    assert by_key["any_step"] == "pending"
    assert by_key["skipped_step"] == "skipped"


def test_timer_fires_once_and_escalates(client, monkeypatch):
    key = _definition(client)
    body = _request(client, key)
    item = next(step for step in body["checklist"] if step["step_key"] == "independent")
    session = db.SessionLocal()
    try:
        timer = session.execute(
            select(db.WorkflowTimerRecord).where(
                db.WorkflowTimerRecord.item_id == item["id"]
            )
        ).scalar_one()
        fired = timers.fire_due(session, now=timer.due_at + timedelta(minutes=2))
        session.commit()
        assert len(fired) == 1
        assert session.get(db.WorkflowTimerRecord, timer.id).status == "fired"
        logs = client.get("/execution-logs").json()
        assert any(row["action"] == "onboarding.step.overdue" for row in logs)
    finally:
        session.close()


def test_offboarding_dag_enforces_prerequisites(client):
    created = client.post(
        "/onboarding-requests",
        json={
            "request_type": "offboarding",
            "employee_name": "Dana",
            "employee_email": "dana@example.com",
            "department": "Eng",
            "requester_id": 1,
            "template": "offboarding-dag",
        },
    )
    assert created.status_code == 201
    request_id = created.json()["id"]
    blocked = client.post(
        f"/offboarding-requests/{request_id}/steps/revoke_licenses/complete"
    )
    assert blocked.status_code == 409
    assert "prerequisites" in blocked.json()["detail"]
    unknown = client.post(
        f"/offboarding-requests/{request_id}/steps/not-a-step/complete"
    )
    assert unknown.status_code == 422


def test_definition_migration_preview_and_apply(client):
    key = _definition(client)
    body = _request(client, key)
    new_steps = [dict(step) for step in STEPS]
    new_steps.append(
        {
            "key": "extra_step",
            "step": "Extra step",
            "queue": "it-service-desk",
            "requires_approval": False,
            "action": "none",
            "depends_on": [],
            "join": "all",
            "when": [],
        }
    )
    client.put(f"/workflow-definitions/{key}", json={"steps": new_steps})
    client.post(f"/workflow-definitions/{key}/publish", json={"version": 2})
    preview = client.post(
        f"/onboarding-requests/{body['id']}/definition",
        json={"version": 2, "dry_run": True},
    )
    assert preview.status_code == 200
    assert "extra_step" in preview.json()["added"]
    applied = client.post(
        f"/onboarding-requests/{body['id']}/definition",
        json={"version": 2, "dry_run": False},
    )
    assert applied.status_code == 200
    current = client.get(f"/onboarding-requests/{body['id']}").json()
    assert current["definition_version"] == 2
    assert any(step["step_key"] == "extra_step" for step in current["checklist"])
