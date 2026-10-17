"""PostgreSQL coverage for the fenced idempotency reservation.

These tests only run when ``FLOWOPS_TEST_POSTGRES_URL`` points at a disposable
PostgreSQL database. CI runs them against a ``postgres:16`` service; the normal
offline suite skips the module and continues to use temporary SQLite files.
"""

import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from src import api, db

POSTGRES_URL = os.environ.get("FLOWOPS_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL, reason="FLOWOPS_TEST_POSTGRES_URL is not set"
)

VALID_REQUEST = {
    "userId": 7,
    "id": 42,
    "title": "Laptop will not start",
    "completed": False,
}

VALID_ONBOARDING = {
    "request_type": "onboarding",
    "employee_name": "Jane Doe",
    "employee_email": "jane@example.com",
    "department": "Sales",
    "requester_id": 3,
    "start_date": "2026-11-02",
}


def _request_hash(payload=None):
    payload = VALID_REQUEST if payload is None else payload
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@pytest.fixture
def client():
    db.use_database(POSTGRES_URL)
    db.Base.metadata.drop_all(bind=db.engine)
    db.Base.metadata.create_all(bind=db.engine)
    try:
        yield TestClient(api.app)
    finally:
        db.Base.metadata.drop_all(bind=db.engine)


def _reservation(key):
    session = db.SessionLocal()
    try:
        record = session.get(db.IdempotencyRecord, key)
        if record is None:
            return None
        return {
            "payload": json.loads(record.response_json),
            "owner_token": record.owner_token,
            "created_at": record.created_at,
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


def test_postgres_claim_takeover_and_completion_are_fenced(client):
    """The PostgreSQL upsert/update/delete path enforces the same fencing rules."""
    request_hash = _request_hash()
    key = "pg-fence-key"
    ticket = api.Ticket(
        ticket_id="42",
        requester_id=7,
        title="Laptop will not start",
        status="open",
        priority="normal",
        category="hardware",
        queue="it-hardware",
    )

    session = db.SessionLocal()
    try:
        scope = api.IDEMPOTENCY_SCOPE_TICKET
        token_a = api._claim_idempotency_key(session, key, "42", request_hash, scope)
        assert token_a is not None
        # A live reservation cannot be claimed twice.
        assert (
            api._claim_idempotency_key(session, key, "42", request_hash, scope)
            is None
        )

        record = session.get(db.IdempotencyRecord, key)
        record.created_at = db.utcnow() - timedelta(minutes=10)
        session.commit()

        token_b = api._claim_idempotency_key(session, key, "42", request_hash, scope)
        assert token_b is not None
        assert token_b != token_a

        # The stale owner can neither complete nor delete the new owner's row.
        assert (
            api._complete_idempotency_key(
                session,
                key,
                token_a,
                request_hash,
                scope,
                "42",
                {"ticket": ticket.model_dump()},
            )
            is False
        )
        session.expire_all()
        stored = session.get(db.IdempotencyRecord, key)
        assert stored.owner_token == token_b
        assert json.loads(stored.response_json)["state"] == "processing"

        assert api._release_idempotency_key(session, key, token_a) is False
        session.expire_all()
        assert session.get(db.IdempotencyRecord, key) is not None

        # The current owner finishes and its completed response is preserved.
        assert (
            api._complete_idempotency_key(
                session,
                key,
                token_b,
                request_hash,
                scope,
                "42",
                {"ticket": ticket.model_dump()},
            )
            is True
        )
        session.commit()
        assert api._release_idempotency_key(session, key, token_b) is False

        session.expire_all()
        stored = session.get(db.IdempotencyRecord, key)
        assert stored.owner_token == token_b
        payload = json.loads(stored.response_json)
        assert payload["state"] == "completed"
        assert payload["resource_type"] == scope
        assert payload["ticket"]["ticket_id"] == "42"
    finally:
        session.close()


def test_postgres_stale_owner_cannot_finalize_after_takeover(client, monkeypatch):
    """End-to-end takeover: only the new owner's ticket and logs can commit."""
    a_entered = threading.Event()
    release_a = threading.Event()
    calls = 0
    call_lock = threading.Lock()
    original_process = api._process_intake

    def pause_first_request(request, session, actor_subject=None):
        nonlocal calls
        with call_lock:
            calls += 1
            is_first = calls == 1
        if is_first:
            a_entered.set()
            assert release_a.wait(timeout=5), "test did not release the stale owner"
        return original_process(request, session, actor_subject)

    monkeypatch.setattr(api, "_process_intake", pause_first_request)
    key = "pg-takeover-key"
    headers = {"Idempotency-Key": key}

    with ThreadPoolExecutor(max_workers=1) as executor:
        stale_future = executor.submit(
            client.post, "/tickets", json=VALID_REQUEST, headers=headers
        )
        assert a_entered.wait(timeout=5), "stale owner never claimed the reservation"
        claimed = _reservation(key)
        assert claimed["payload"]["state"] == "processing"
        _make_reservation_stale(key)

        new_owner = client.post("/tickets", json=VALID_REQUEST, headers=headers)
        assert new_owner.status_code == 201
        after_takeover = _reservation(key)
        assert after_takeover["payload"]["state"] == "completed"
        assert after_takeover["owner_token"] != claimed["owner_token"]

        release_a.set()
        stale_response = stale_future.result(timeout=5)

    assert stale_response.status_code == 409
    assert "took over" in stale_response.json()["detail"]
    assert len(client.get("/tickets").json()) == 1
    actions = [log["action"] for log in client.get("/execution-logs").json()]
    assert actions.count("ticket.created") == 1
    assert "ticket.updated" not in actions

    final = _reservation(key)
    assert final["payload"]["state"] == "completed"
    assert final["owner_token"] == after_takeover["owner_token"]
    replay = client.post("/tickets", json=VALID_REQUEST, headers=headers)
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == new_owner.json()


def test_postgres_additive_fencing_column_migration(client):
    """The additive ALTER used for pre-fencing databases is valid on PostgreSQL."""
    with db.engine.begin() as connection:
        connection.execute(text("ALTER TABLE idempotency_keys DROP COLUMN owner_token"))

    db._ensure_idempotency_owner_token()

    columns = {
        column["name"]
        for column in inspect(db.engine).get_columns("idempotency_keys")
    }
    assert "owner_token" in columns


def test_postgres_additive_resource_scope_migration(client):
    """The additive scope column migration is valid on PostgreSQL."""
    with db.engine.begin() as connection:
        connection.execute(text("ALTER TABLE idempotency_keys DROP COLUMN resource_type"))

    db._ensure_idempotency_resource_type()

    columns = {
        column["name"]
        for column in inspect(db.engine).get_columns("idempotency_keys")
    }
    assert "resource_type" in columns


def test_postgres_onboarding_stale_owner_cannot_finalize_after_takeover(
    client, monkeypatch
):
    """The scoped fence works for onboarding requests on PostgreSQL."""
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
    key = "pg-onboarding-takeover"
    headers = {"Idempotency-Key": key}

    with ThreadPoolExecutor(max_workers=1) as executor:
        stale_future = executor.submit(
            client.post, "/onboarding-requests", json=VALID_ONBOARDING, headers=headers
        )
        assert a_entered.wait(timeout=5), "stale owner never claimed the reservation"
        claimed = _reservation(key)
        assert claimed["payload"]["state"] == "processing"
        _make_reservation_stale(key)

        new_owner = client.post(
            "/onboarding-requests", json=VALID_ONBOARDING, headers=headers
        )
        assert new_owner.status_code == 201
        after_takeover = _reservation(key)
        assert after_takeover["payload"]["state"] == "completed"
        assert after_takeover["owner_token"] != claimed["owner_token"]

        release_a.set()
        stale_response = stale_future.result(timeout=5)

    assert stale_response.status_code == 409
    assert len(client.get("/onboarding-requests").json()) == 1
    replay = client.post(
        "/onboarding-requests", json=VALID_ONBOARDING, headers=headers
    )
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == new_owner.json()


def test_postgres_ticket_and_onboarding_keys_are_scoped(client):
    key = "pg-shared-scope-key"
    ticket = client.post("/tickets", json=VALID_REQUEST, headers={"Idempotency-Key": key})
    assert ticket.status_code == 201

    onboarding_conflict = client.post(
        "/onboarding-requests",
        json=VALID_ONBOARDING,
        headers={"Idempotency-Key": key},
    )
    assert onboarding_conflict.status_code == 409
    assert "different resource type" in onboarding_conflict.json()["detail"]
