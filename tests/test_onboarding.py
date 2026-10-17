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
        {"start_date": "November 2"},
        {"request_type": "transfer"},
        {"requester_id": 0},
    ],
)
def test_invalid_onboarding_requests_rejected(client, overrides):
    assert _create(client, overrides).status_code == 422


@pytest.mark.parametrize(
    "bad_date",
    [
        "2026-13-01",   # month 13 does not exist
        "2026-02-30",   # February never has 30 days
        "2026-04-31",   # April has 30 days
        "2024-02-29x",  # trailing junk after a real leap date
        "2026-2-3",     # right idea, wrong format (no zero padding)
        "20260315",     # compact form is not ISO YYYY-MM-DD
    ],
)
def test_impossible_or_malformed_dates_rejected(client, bad_date):
    """ISO-shaped but impossible calendar dates must fail validation."""
    response = _create(client, {"start_date": bad_date})
    assert response.status_code == 422


def test_real_leap_day_accepted(client):
    """2024 is a leap year, so Feb 29 is a valid calendar date."""
    response = _create(client, {"start_date": "2024-02-29"})
    assert response.status_code == 201
    assert response.json()["start_date"] == "2024-02-29"


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


# ---------------------------------------------------------------------------
# v0.6 placeholder: simulated Microsoft 365 provisioning (offline, no network)


class RecordingSimulator:
    """Test double for the M365 simulator; counts calls, returns fake data."""

    def __init__(self):
        self.calls: list[dict] = []

    def create_user(self, display_name: str, mail: str, department: str) -> dict:
        self.calls.append(
            {"display_name": display_name, "mail": mail, "department": department}
        )
        return {
            "simulated": True,
            "id": f"fake-id-{len(self.calls)}",
            "display_name": display_name,
            "mail": mail,
            "department": department,
        }

    def get_status(self, user_id: str) -> dict:
        return {
            "simulated": True,
            "id": user_id,
            "provisioning_state": "success",
            "last_sync": "2026-01-01T00:00:00+00:00",
        }


@pytest.fixture
def recording_simulator(monkeypatch):
    fake = RecordingSimulator()
    monkeypatch.setattr(api, "m365_simulator", fake)
    return fake


def _complete_m365_step(client, body):
    step = next(i for i in body["checklist"] if i["action"] == "m365_provision")
    return client.post(
        f"/onboarding-requests/{body['id']}/items/{step['id']}/complete"
    )


def test_m365_step_added_only_with_mail(client):
    with_mail = _create(client, {"mail": "jane@contoso.example"})
    assert with_mail.status_code == 201
    steps = with_mail.json()["checklist"]
    assert len(steps) == len(ONBOARDING_STEPS) + 1
    m365_steps = [s for s in steps if s["action"] == "m365_provision"]
    assert len(m365_steps) == 1
    assert m365_steps[0]["result"] is None  # not provisioned until completed

    without_mail = _create(client, {"employee_email": "noone@example.com"})
    assert without_mail.status_code == 201
    assert all(s["action"] != "m365_provision" for s in without_mail.json()["checklist"])


def test_m365_offboarding_never_gets_provision_step(client):
    response = _create(
        client,
        {
            "request_type": "offboarding",
            "employee_name": "Bob Roe",
            "employee_email": "bob@example.com",
            "mail": "bob@contoso.example",
        },
    )
    assert response.status_code == 201
    assert all(s["action"] != "m365_provision" for s in response.json()["checklist"])


@pytest.mark.parametrize("bad_mail", ["not-an-email", "a b@x.io", "@missing.local", "no.tld@domain"])
def test_invalid_mail_rejected_422(client, bad_mail):
    assert _create(client, {"mail": bad_mail}).status_code == 422


def test_completing_m365_step_calls_simulator_once(client, recording_simulator):
    body = _create(client, {"mail": "jane@contoso.example"}).json()
    done = _complete_m365_step(client, body)
    assert done.status_code == 200
    assert len(recording_simulator.calls) == 1
    assert recording_simulator.calls[0] == {
        "display_name": "Jane Doe",
        "mail": "jane@example.com",
        "department": "Sales",
    }
    step = next(i for i in done.json()["checklist"] if i["action"] == "m365_provision")
    assert step["status"] == "completed"
    assert step["result"]["simulated"] is True
    assert step["result"]["id"] == "fake-id-1"


def test_completed_m365_step_not_rerun_on_later_transitions(client, recording_simulator):
    body = _create(client, {"mail": "jane@contoso.example"}).json()
    _complete_m365_step(client, body)
    step = next(i for i in body["checklist"] if i["action"] == "m365_provision")
    again = client.post(
        f"/onboarding-requests/{body['id']}/items/{step['id']}/complete"
    )
    assert again.status_code == 409  # already completed
    assert len(recording_simulator.calls) == 1  # never re-executed


def test_m365_stored_result_is_returned_without_recalling_simulator(client, recording_simulator):
    """The simulated user id is stored once; re-reading never re-provisions."""
    body = _create(client, {"mail": "jane@contoso.example"}).json()
    done = _complete_m365_step(client, body)
    stored_user_id = next(
        i for i in done.json()["checklist"] if i["action"] == "m365_provision"
    )["result"]["id"]
    assert len(recording_simulator.calls) == 1
    # Re-reads (GET) return the same stored result and never call again.
    fetched = client.get(f"/onboarding-requests/{body['id']}").json()
    replayed_step = next(
        i for i in fetched["checklist"] if i["action"] == "m365_provision"
    )
    assert replayed_step["result"]["id"] == stored_user_id
    listed = client.get("/onboarding-requests").json()
    listed_step = next(
        i for item in listed for i in item["checklist"] if i["action"] == "m365_provision"
    )
    assert listed_step["result"]["id"] == stored_user_id
    assert len(recording_simulator.calls) == 1  # reads never re-provision


def test_ticket_idempotency_replay_untouched_by_m365(client):
    """Requirement 4 regression: /tickets replay still works with v0.6 code."""
    payload = {"userId": 1, "id": 42, "title": "Printer jam", "completed": False}
    headers = {"Idempotency-Key": "same-key"}
    first = client.post("/tickets", json=payload, headers=headers)
    assert first.status_code == 201
    second = client.post("/tickets", json=payload, headers=headers)
    assert second.status_code == 200
    assert second.headers["Idempotency-Replayed"] == "true"
    assert second.json() == first.json()


def test_m365_status_endpoint_shape(client):
    response = client.get("/onboarding-requests/m365-status/abc-123")
    assert response.status_code == 200
    body = response.json()
    assert body["simulated"] is True
    assert body["id"] == "abc-123"
    assert body["provisioning_state"] == "success"


def test_real_simulator_is_deterministic_and_labelled():
    from src.m365_simulator import M365Simulator

    sim = M365Simulator()
    a = sim.create_user("Jane Doe", "Jane@Contoso.Example", "Sales")
    b = sim.create_user("Jane Doe", "jane@contoso.example", "Sales")
    assert a["simulated"] is True and a["id"] == b["id"]  # same mail -> same id
    assert len(a["id"]) == 36  # GUID-shaped (8-4-4-4-12)
    status = sim.get_status(a["id"])
    assert status["simulated"] is True and status["id"] == a["id"]


def test_m365_provision_logged(client, recording_simulator):
    body = _create(client, {"mail": "jane@contoso.example"}).json()
    _complete_m365_step(client, body)
    logs = client.get("/execution-logs").json()
    provision_logs = [log for log in logs if log["action"] == "onboarding.m365_provision"]
    assert len(provision_logs) == 1
    assert provision_logs[0]["status"] == "success"
    assert "fake-id-1" in provision_logs[0]["detail"]


def test_m365_dashboard_section_escapes_and_labels(client, monkeypatch):
    class EvilSimulator:
        def create_user(self, display_name, mail, department):
            return {"simulated": True, "id": "<script>alert(1)</script>", "mail": mail}

        def get_status(self, user_id):
            return {}

    monkeypatch.setattr(api, "m365_simulator", EvilSimulator())
    body = _create(client, {"mail": "jane@contoso.example"}).json()
    _complete_m365_step(client, body)
    page = client.get("/dashboard").text
    assert "Simulated Microsoft 365 provisioning" in page
    assert "(simulated)" in page
    assert "<script>alert(1)</script>" not in page  # escaped, not injected


def test_m365_failure_rolls_back_and_logs(client, monkeypatch):
    class FailingSimulator:
        def create_user(self, display_name, mail, department):
            raise RuntimeError("simulated outage")

        def get_status(self, user_id):
            return {}

    monkeypatch.setattr(api, "m365_simulator", FailingSimulator())
    body = _create(client, {"mail": "jane@contoso.example"}).json()
    failed = _complete_m365_step(client, body)
    assert failed.status_code == 502
    fetched = client.get(f"/onboarding-requests/{body['id']}").json()
    step = next(i for i in fetched["checklist"] if i["action"] == "m365_provision")
    assert step["status"] == "pending"  # rolled back, retryable later
    assert fetched["status"] == "in_progress"
    logs = client.get("/execution-logs").json()
    failure_logs = [
        log for log in logs
        if log["action"] == "onboarding.m365_provision" and log["status"] == "failure"
    ]
    assert len(failure_logs) == 1
    assert "simulated outage" in failure_logs[0]["detail"]
