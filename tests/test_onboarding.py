"""Tests for onboarding/offboarding workflows (v0.5)."""

import pytest
from fastapi.testclient import TestClient

from src import api, db
from src.onboarding import OFFBOARDING_STEPS, ONBOARDING_STEPS

VALID_ONBOARDING = {
    "request_type": "onboarding",
    "employee_name": "Jane Doe",
    "employee_email": "Jane@Example.com",
    "department": "Sales",
    "requester_id": 3,
    "start_date": "2026-11-02",
}


@pytest.fixture
def client(tmp_path):
    db.use_database(f"sqlite:///{tmp_path}/test.db")
    return TestClient(api.app)


def _create(client, overrides=None):
    payload = {**VALID_ONBOARDING, **(overrides or {})}
    return client.post("/onboarding-requests", json=payload)


def test_create_onboarding_generates_checklist(client):
    response = _create(client)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "in_progress"
    assert body["employee_email"] == "jane@example.com"  # normalized
    assert len(body["checklist"]) == len(ONBOARDING_STEPS)
    assert all(item["status"] == "pending" for item in body["checklist"])


def test_create_offboarding_uses_offboarding_template(client):
    response = _create(client, {"request_type": "offboarding"})
    assert response.status_code == 201
    steps = [item["step"] for item in response.json()["checklist"]]
    assert steps == [template["step"] for template in OFFBOARDING_STEPS]


@pytest.mark.parametrize(
    "overrides",
    [
        {"employee_email": "not-an-email"},
        {"employee_name": "   "},
        {"start_date": "November 2"},     # wrong format (not ISO)
        {"request_type": "transfer"},
        {"requester_id": 0},
        # --- Scenario 3: impossible calendar dates ---
        # These strings match the YYYY-MM-DD pattern but name days that do not
        # exist on any calendar.  date.fromisoformat() rejects them, so the
        # validator must raise 422 rather than silently accepting the string.
        {"start_date": "2026-02-30"},     # February only has 28 or 29 days
        {"start_date": "2026-13-01"},     # month 13 does not exist
        {"start_date": "2026-04-31"},     # April only has 30 days
    ],
)
def test_invalid_onboarding_requests_rejected(client, overrides):
    assert _create(client, overrides).status_code == 422


def test_valid_edge_case_dates_are_accepted(client):
    """Scenario 3 (positive path): real dates near boundaries must not be rejected."""
    # Feb 28 in a non-leap year — always valid.
    assert _create(client, {"start_date": "2026-02-28"}).status_code == 201
    # Feb 29 in a leap year — should also be accepted.
    assert _create(
        client,
        {"start_date": "2024-02-29", "employee_email": "other@example.com"},
    ).status_code == 201
    # None (omitted date) must also be accepted.
    assert _create(
        client,
        {"start_date": None, "employee_email": "third@example.com"},
    ).status_code == 201


def test_approval_required_before_completion(client):
    request_id = _create(client).json()["id"]
    needs_approval = next(
        item for item in client.get(f"/onboarding-requests/{request_id}").json()["checklist"]
        if item["requires_approval"]
    )
    complete = client.post(f"/onboarding-requests/{request_id}/items/{needs_approval['id']}/complete")
    assert complete.status_code == 409


def test_approving_a_non_approval_item_conflicts(client):
    request_id = _create(client).json()["id"]
    no_approval = next(
        item for item in client.get(f"/onboarding-requests/{request_id}").json()["checklist"]
        if not item["requires_approval"]
    )
    approve = client.post(f"/onboarding-requests/{request_id}/items/{no_approval['id']}/approve")
    assert approve.status_code == 409


def test_full_checklist_flow_completes_request(client):
    request_id = _create(client).json()["id"]
    checklist = client.get(f"/onboarding-requests/{request_id}").json()["checklist"]
    for item in checklist:
        if item["requires_approval"]:
            approved = client.post(
                f"/onboarding-requests/{request_id}/items/{item['id']}/approve"
            )
            assert approved.status_code == 200
            updated = next(
                i for i in approved.json()["checklist"] if i["id"] == item["id"]
            )
            assert updated["status"] == "approved"
        done = client.post(f"/onboarding-requests/{request_id}/items/{item['id']}/complete")
        assert done.status_code == 200
    final = client.get(f"/onboarding-requests/{request_id}").json()
    assert final["status"] == "completed"
    assert all(item["status"] == "completed" for item in final["checklist"])


def test_unknown_request_or_item_returns_404(client):
    request_id = _create(client).json()["id"]
    assert client.post("/onboarding-requests/999/items/1/complete").status_code == 404
    assert client.post(f"/onboarding-requests/{request_id}/items/999/complete").status_code == 404


def test_list_includes_checklists(client):
    _create(client)
    _create(client, {"employee_name": "Bob Roe", "employee_email": "bob@example.com"})
    listed = client.get("/onboarding-requests").json()
    assert len(listed) == 2
    assert all(len(item["checklist"]) == len(ONBOARDING_STEPS) for item in listed)


def test_onboarding_actions_logged(client):
    request_id = _create(client).json()["id"]
    item = client.get(f"/onboarding-requests/{request_id}").json()["checklist"][0]
    if item["requires_approval"]:
        client.post(f"/onboarding-requests/{request_id}/items/{item['id']}/approve")
    client.post(f"/onboarding-requests/{request_id}/items/{item['id']}/complete")
    actions = [log["action"] for log in client.get("/execution-logs").json()]
    assert "onboarding.request.created" in actions
    assert any(action.startswith("onboarding.item.") for action in actions)
