"""Tests for onboarding/offboarding workflows (v0.5)."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

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

VALID_TICKET = {
    "userId": 7,
    "id": 42,
    "title": "Laptop will not start",
    "completed": False,
}


@pytest.fixture
def client(tmp_path):
    db.use_database(f"sqlite:///{tmp_path}/test.db")
    return TestClient(api.app)


def _create(client, overrides=None):
    payload = {**VALID_ONBOARDING, **(overrides or {})}
    return client.post("/onboarding-requests", json=payload)


def _ensure_tables():
    db.Base.metadata.create_all(bind=db.engine)


def _reservation(key):
    session = db.SessionLocal()
    try:
        record = session.get(db.IdempotencyRecord, key)
        if record is None:
            return None
        return {
            "payload": json.loads(record.response_json),
            "owner_token": record.owner_token,
            "resource_type": record.resource_type,
        }
    finally:
        session.close()


def _make_reservation_stale(key):
    session = db.SessionLocal()
    try:
        record = session.get(db.IdempotencyRecord, key)
        assert record is not None
        record.created_at = db.utcnow() - timedelta(minutes=10)
        session.commit()
    finally:
        session.close()


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


def test_get_onboarding_request_status_summary(client):
    """Initial request status reflects pending approval and zero completion."""
    request_id = _create(client).json()["id"]
    response = client.get(f"/onboarding-requests/{request_id}/status")
    assert response.status_code == 200
    summary = response.json()
    assert summary["id"] == request_id
    assert summary["request_type"] == "onboarding"
    assert summary["employee_name"] == "Jane Doe"
    assert summary["status"] == "in_progress"
    assert summary["total_steps"] == len(ONBOARDING_STEPS)
    assert summary["completed_steps"] == 0
    # Onboarding has 2 steps requiring approval (account & licenses)
    assert summary["pending_approval_steps"] == 2
    assert summary["percent_complete"] == 0


def test_get_onboarding_request_status_progresses_to_completion(client):
    """Status summary updates as steps are approved and completed."""
    request_id = _create(client).json()["id"]
    checklist = client.get(f"/onboarding-requests/{request_id}").json()["checklist"]

    # Approve one item requiring approval
    gated_item = next(item for item in checklist if item["requires_approval"])
    client.post(f"/onboarding-requests/{request_id}/items/{gated_item['id']}/approve")
    summary = client.get(f"/onboarding-requests/{request_id}/status").json()
    assert summary["pending_approval_steps"] == 1
    assert summary["completed_steps"] == 0

    # Complete that item
    client.post(f"/onboarding-requests/{request_id}/items/{gated_item['id']}/complete")
    summary = client.get(f"/onboarding-requests/{request_id}/status").json()
    assert summary["completed_steps"] == 1
    assert summary["percent_complete"] == 25

    # Complete the rest
    for item in checklist:
        if item["id"] == gated_item["id"]:
            continue
        if item["requires_approval"]:
            client.post(f"/onboarding-requests/{request_id}/items/{item['id']}/approve")
        client.post(f"/onboarding-requests/{request_id}/items/{item['id']}/complete")

    final = client.get(f"/onboarding-requests/{request_id}/status").json()
    assert final["status"] == "completed"
    assert final["completed_steps"] == len(ONBOARDING_STEPS)
    assert final["pending_approval_steps"] == 0
    assert final["percent_complete"] == 100


def test_get_onboarding_request_status_unknown_returns_404(client):
    assert client.get("/onboarding-requests/999/status").status_code == 404


# --- Onboarding idempotency (fenced reservations) ---


def test_onboarding_idempotency_key_replays_stored_response(client):
    headers = {"Idempotency-Key": "onboarding-replay"}
    first = client.post("/onboarding-requests", json=VALID_ONBOARDING, headers=headers)
    second = client.post("/onboarding-requests", json=VALID_ONBOARDING, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.headers["Idempotency-Replayed"] == "true"
    assert second.json() == first.json()
    assert len(client.get("/onboarding-requests").json()) == 1

    actions = [log["action"] for log in client.get("/execution-logs").json()]
    assert actions.count("onboarding.request.created") == 1
    assert "onboarding.request.idempotent_replay" in actions


def test_onboarding_same_key_different_payload_returns_409(client):
    headers = {"Idempotency-Key": "onboarding-conflict"}
    first = client.post("/onboarding-requests", json=VALID_ONBOARDING, headers=headers)
    assert first.status_code == 201

    conflict = client.post(
        "/onboarding-requests",
        json={**VALID_ONBOARDING, "employee_name": "Someone Else"},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert "different request" in conflict.json()["detail"]
    assert len(client.get("/onboarding-requests").json()) == 1


def test_onboarding_concurrent_same_key_returns_409(client, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    call_lock = threading.Lock()
    original_create = api._create_onboarding_request

    def pause_first(request, session, actor_subject=None):
        nonlocal calls
        with call_lock:
            calls += 1
            is_first = calls == 1
        if is_first:
            entered.set()
            assert release.wait(timeout=5), "test did not release the first request"
        return original_create(request, session, actor_subject)

    monkeypatch.setattr(api, "_create_onboarding_request", pause_first)
    headers = {"Idempotency-Key": "onboarding-concurrent"}

    with ThreadPoolExecutor(max_workers=1) as executor:
        first_future = executor.submit(
            client.post, "/onboarding-requests", json=VALID_ONBOARDING, headers=headers
        )
        assert entered.wait(timeout=5), "first request never reached creation"
        try:
            concurrent = client.post(
                "/onboarding-requests", json=VALID_ONBOARDING, headers=headers
            )
        finally:
            release.set()
        first = first_future.result(timeout=5)

    assert concurrent.status_code == 409
    assert "still processing" in concurrent.json()["detail"]
    assert first.status_code == 201
    assert len(client.get("/onboarding-requests").json()) == 1


def test_onboarding_stale_owner_cannot_finalize_after_takeover(client, monkeypatch):
    """Only the new owner's onboarding record and logs may commit."""
    _ensure_tables()
    a_entered = threading.Event()
    release_a = threading.Event()
    calls = 0
    call_lock = threading.Lock()
    original_create = api._create_onboarding_request

    def pause_first(request, session, actor_subject=None):
        nonlocal calls
        with call_lock:
            calls += 1
            is_first = calls == 1
        if is_first:
            a_entered.set()
            assert release_a.wait(timeout=5), "test did not release the stale owner"
        return original_create(request, session, actor_subject)

    monkeypatch.setattr(api, "_create_onboarding_request", pause_first)
    key = "onboarding-takeover"
    headers = {"Idempotency-Key": key}

    with ThreadPoolExecutor(max_workers=1) as executor:
        stale_future = executor.submit(
            client.post, "/onboarding-requests", json=VALID_ONBOARDING, headers=headers
        )
        assert a_entered.wait(timeout=5), "stale owner never claimed the reservation"
        claimed = _reservation(key)
        assert claimed["payload"]["state"] == "processing"
        assert claimed["owner_token"]
        assert claimed["resource_type"] == api.IDEMPOTENCY_SCOPE_ONBOARDING
        _make_reservation_stale(key)

        new_owner = client.post(
            "/onboarding-requests", json=VALID_ONBOARDING, headers=headers
        )
        assert new_owner.status_code == 201
        after_takeover = _reservation(key)
        assert after_takeover["payload"]["state"] == "completed"
        assert after_takeover["owner_token"] != claimed["owner_token"]
        assert after_takeover["payload"]["resource_type"] == "onboarding"

        release_a.set()
        stale_response = stale_future.result(timeout=5)

    assert stale_response.status_code == 409
    assert "took over" in stale_response.json()["detail"]
    assert len(client.get("/onboarding-requests").json()) == 1
    actions = [log["action"] for log in client.get("/execution-logs").json()]
    assert actions.count("onboarding.request.created") == 1

    final = _reservation(key)
    assert final["payload"]["state"] == "completed"
    assert final["owner_token"] == after_takeover["owner_token"]
    replay = client.post(
        "/onboarding-requests", json=VALID_ONBOARDING, headers=headers
    )
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == new_owner.json()


def test_onboarding_failure_cleans_up_only_its_own_reservation(client, monkeypatch):
    _ensure_tables()
    session = db.SessionLocal()
    try:
        session.add(
            db.IdempotencyRecord(
                key="foreign-onboarding-key",
                ticket_id="0",
                response_json=json.dumps(
                    {"request_hash": "other", "state": "processing"},
                    separators=(",", ":"),
                ),
                owner_token="foreign-owner-token",
                resource_type=api.IDEMPOTENCY_SCOPE_ONBOARDING,
                created_at=db.utcnow(),
            )
        )
        session.commit()
    finally:
        session.close()

    def fail_create(request, session, actor_subject=None):
        raise RuntimeError("onboarding creation exploded")

    monkeypatch.setattr(api, "_create_onboarding_request", fail_create)
    with pytest.raises(RuntimeError, match="onboarding creation exploded"):
        client.post(
            "/onboarding-requests",
            json=VALID_ONBOARDING,
            headers={"Idempotency-Key": "onboarding-failing"},
        )

    assert _reservation("onboarding-failing") is None
    foreign = _reservation("foreign-onboarding-key")
    assert foreign is not None
    assert foreign["payload"]["state"] == "processing"
    assert foreign["owner_token"] == "foreign-owner-token"


def test_ticket_and_onboarding_keys_are_scoped(client):
    ticket_key = "shared-ticket-first"
    ticket = client.post(
        "/tickets", json=VALID_TICKET, headers={"Idempotency-Key": ticket_key}
    )
    assert ticket.status_code == 201
    onboarding_conflict = client.post(
        "/onboarding-requests",
        json=VALID_ONBOARDING,
        headers={"Idempotency-Key": ticket_key},
    )
    assert onboarding_conflict.status_code == 409
    assert "different resource type" in onboarding_conflict.json()["detail"]

    onboarding_key = "shared-onboarding-first"
    onboarding = client.post(
        "/onboarding-requests",
        json=VALID_ONBOARDING,
        headers={"Idempotency-Key": onboarding_key},
    )
    assert onboarding.status_code == 201
    ticket_conflict = client.post(
        "/tickets", json=VALID_TICKET, headers={"Idempotency-Key": onboarding_key}
    )
    assert ticket_conflict.status_code == 409
    assert "different resource type" in ticket_conflict.json()["detail"]
