"""Tests for the FlowOps API: intake, routing, idempotency, webhooks, logs.

Fully offline: SQLite in a temp directory, HTTP via TestClient.
"""

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from src import api, db

VALID_REQUEST = {
    "userId": 7,
    "id": 42,
    "title": "Laptop will not start",
    "completed": False,
}


@pytest.fixture
def client(tmp_path):
    db.use_database(f"sqlite:///{tmp_path}/test.db")
    return TestClient(api.app)


# --- Service routes ---


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_root_returns_html_landing_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "FlowOps" in response.text
    assert "/docs" in response.text


def test_landing_page_has_no_merge_conflict_markers(client):
    page = client.get("/").text
    for marker in ("<<<<<<<", "=======", ">>>>>>>"):
        assert marker not in page
    assert "/onboarding-requests" in page
    assert "/ui/notifications" in page
    assert "/ui/onboarding-requests" in page


# --- Ticket intake ---


def test_valid_ticket_is_routed_and_stored(client):
    response = client.post("/tickets", json=VALID_REQUEST)
    assert response.status_code == 201
    assert response.json() == {
        "ticket_id": "42",
        "requester_id": 7,
        "title": "Laptop will not start",
        "status": "open",
        "priority": "normal",
        "category": "hardware",
        "queue": "it-hardware",
    }


def test_completed_ticket_maps_to_closed(client):
    response = client.post("/tickets", json={**VALID_REQUEST, "completed": True})
    assert response.json()["status"] == "closed"


def test_urgent_network_ticket_routed_correctly(client):
    response = client.post(
        "/tickets",
        json={**VALID_REQUEST, "id": 43, "title": "VPN outage — all users cannot work"},
    )
    body = response.json()
    assert body["category"] == "network"
    assert body["priority"] == "urgent"
    assert body["queue"] == "it-network"


def test_resubmitting_same_id_updates_ticket(client):
    first = client.post("/tickets", json=VALID_REQUEST)
    second = client.post("/tickets", json={**VALID_REQUEST, "title": "Updated title"})
    assert (first.status_code, second.status_code) == (201, 200)
    assert client.get("/tickets/42").json()["title"] == "Updated title"


@pytest.mark.parametrize(
    "payload",
    [
        {k: v for k, v in VALID_REQUEST.items() if k != "userId"},  # missing field
        {**VALID_REQUEST, "userId": "7"},  # string instead of int
        {**VALID_REQUEST, "id": True},  # bool instead of int
        {**VALID_REQUEST, "completed": 1},  # int instead of bool
        {**VALID_REQUEST, "title": 123},  # int instead of str
        {**VALID_REQUEST, "title": ""},  # empty title
        {**VALID_REQUEST, "title": "   "},  # whitespace-only title
        {**VALID_REQUEST, "id": 0},  # non-positive id
        ["not", "an", "object"],  # wrong top-level structure
    ],
)
def test_invalid_requests_rejected_with_422(client, payload):
    assert client.post("/tickets", json=payload).status_code == 422


def test_get_ticket_returns_stored_ticket(client):
    client.post("/tickets", json=VALID_REQUEST)
    response = client.get("/tickets/42")
    assert response.status_code == 200
    assert response.json()["ticket_id"] == "42"


def test_get_unknown_ticket_returns_404(client):
    assert client.get("/tickets/999").status_code == 404


def test_get_non_numeric_ticket_id_returns_404(client):
    # Non-digit IDs (including path traversal attempts) must never resolve
    # to a file path or database lookup.
    assert client.get("/tickets/not-a-number").status_code == 404
    assert client.get("/tickets/%2e%2e").status_code == 404


def test_list_tickets_sorted_by_id(client):
    for ticket_id in (17, 3, 8):
        client.post("/tickets", json={**VALID_REQUEST, "id": ticket_id})
    ids = [ticket["ticket_id"] for ticket in client.get("/tickets").json()]
    assert ids == ["3", "8", "17"]


# --- Idempotency (v0.5) ---


def _request_hash(payload=None):
    payload = VALID_REQUEST if payload is None else payload
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _ensure_tables():
    db.Base.metadata.create_all(bind=db.engine)


def _reservation(key):
    """Read one reservation directly, outside the request session."""
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
    """Move a reservation's lease into the past without sleeping."""
    session = db.SessionLocal()
    try:
        record = session.get(db.IdempotencyRecord, key)
        assert record is not None, "reservation must exist before it can go stale"
        record.created_at = db.utcnow() - timedelta(minutes=10)
        session.commit()
    finally:
        session.close()


def test_idempotency_key_replays_stored_response(client):
    headers = {"Idempotency-Key": "key-123"}
    first = client.post("/tickets", json=VALID_REQUEST, headers=headers)
    second = client.post("/tickets", json=VALID_REQUEST, headers=headers)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.headers["Idempotency-Replayed"] == "true"
    assert second.json() == first.json()
    # The replay must not create a duplicate ticket or a second create-run.
    assert len(client.get("/tickets").json()) == 1
    assert client.get("/execution-logs").json()[0]["action"] == "ticket.idempotent_replay"


def test_different_idempotency_keys_create_separate_tickets(client):
    client.post("/tickets", json=VALID_REQUEST, headers={"Idempotency-Key": "a"})
    client.post(
        "/tickets", json={**VALID_REQUEST, "id": 43}, headers={"Idempotency-Key": "b"}
    )
    assert len(client.get("/tickets").json()) == 2


def test_overlong_idempotency_key_rejected(client):
    response = client.post(
        "/tickets", json=VALID_REQUEST, headers={"Idempotency-Key": "x" * 129}
    )
    assert response.status_code == 422


def test_same_payload_same_key_replays_original_result(client):
    """Scenario 1: identical payload + same Idempotency-Key returns the stored result.

    The second call must not re-execute the ticket pipeline (no second
    ticket.created entry in the execution log) and must carry the
    Idempotency-Replayed header.
    """
    key = "replay-same-payload"
    headers = {"Idempotency-Key": key}
    first = client.post("/tickets", json=VALID_REQUEST, headers=headers)
    assert first.status_code == 201

    second = client.post("/tickets", json=VALID_REQUEST, headers=headers)
    assert second.status_code == 200
    assert second.headers.get("Idempotency-Replayed") == "true"
    # The replayed body must exactly match the original.
    assert second.json() == first.json()
    # Only one ticket must exist — no duplicate was created.
    assert len(client.get("/tickets").json()) == 1
    # The most recent log entry is the replay, not a second ticket.created.
    logs = client.get("/execution-logs").json()
    assert logs[0]["action"] == "ticket.idempotent_replay"
    # Exactly one ticket.created in the log total (the original request).
    created_entries = [log for log in logs if log["action"] == "ticket.created"]
    assert len(created_entries) == 1


def test_different_payload_same_key_returns_409(client):
    """Scenario 2: different payload with an already-used key must return 409.

    The conflict must be detected before the new request is processed, so
    the existing ticket must remain unchanged and no second create-run may
    be logged.
    """
    key = "conflict-key"
    headers = {"Idempotency-Key": key}
    first = client.post("/tickets", json=VALID_REQUEST, headers=headers)
    assert first.status_code == 201
    original_title = first.json()["title"]

    # Re-use the same key with a deliberately different payload.
    different_payload = {**VALID_REQUEST, "title": "A completely different title"}
    conflict = client.post("/tickets", json=different_payload, headers=headers)
    assert conflict.status_code == 409

    # The stored ticket must still hold the original title.
    stored = client.get("/tickets/42").json()
    assert stored["title"] == original_title

    # No second ticket.created log entry should have appeared.
    logs = client.get("/execution-logs").json()
    created_entries = [log for log in logs if log["action"] == "ticket.created"]
    assert len(created_entries) == 1


def test_concurrent_same_key_returns_in_progress_then_replays(client, monkeypatch):
    """A concurrent duplicate gets 409 while one caller owns the key."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered_pipeline = Event()
    release_pipeline = Event()
    original_process = api._process_intake

    def pause_pipeline(request, session, actor_subject=None):
        entered_pipeline.set()
        assert release_pipeline.wait(timeout=5), "test did not release the first request"
        return original_process(request, session, actor_subject)

    monkeypatch.setattr(api, "_process_intake", pause_pipeline)
    headers = {"Idempotency-Key": "concurrent-same-key"}

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            client.post, "/tickets", json=VALID_REQUEST, headers=headers
        )
        assert entered_pipeline.wait(timeout=5), "first request never reached ticket processing"
        try:
            concurrent = client.post("/tickets", json=VALID_REQUEST, headers=headers)
        finally:
            release_pipeline.set()
        first = first_future.result(timeout=5)

    assert concurrent.status_code == 409
    assert "still processing" in concurrent.json()["detail"]
    assert first.status_code == 201

    replay = client.post("/tickets", json=VALID_REQUEST, headers=headers)
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()
    assert len(client.get("/tickets").json()) == 1
    actions = [log["action"] for log in client.get("/execution-logs").json()]
    assert actions.count("ticket.created") == 1


def test_stale_processing_reservation_is_reclaimed_and_replayed(client):
    """A process crash must not leave a matching idempotency key stuck forever."""
    import hashlib
    import json

    request_hash = hashlib.sha256(
        json.dumps(VALID_REQUEST, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    db.Base.metadata.create_all(bind=db.engine)
    session = db.SessionLocal()
    try:
        session.add(
            db.IdempotencyRecord(
                key="abandoned-key",
                ticket_id=str(VALID_REQUEST["id"]),
                response_json=json.dumps(
                    {"request_hash": request_hash, "state": "processing"},
                    separators=(",", ":"),
                ),
                created_at=db.utcnow() - timedelta(minutes=10),
            )
        )
        session.commit()
    finally:
        session.close()

    headers = {"Idempotency-Key": "abandoned-key"}
    recovered = client.post("/tickets", json=VALID_REQUEST, headers=headers)
    replay = client.post("/tickets", json=VALID_REQUEST, headers=headers)

    assert recovered.status_code == 201
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == recovered.json()
    assert len(client.get("/tickets").json()) == 1


def test_old_completed_idempotency_record_is_never_reclaimed(client):
    headers = {"Idempotency-Key": "old-completed-key"}
    first = client.post("/tickets", json=VALID_REQUEST, headers=headers)
    assert first.status_code == 201

    session = db.SessionLocal()
    try:
        record = session.get(db.IdempotencyRecord, headers["Idempotency-Key"])
        record.created_at = db.utcnow() - timedelta(days=1)
        session.commit()
    finally:
        session.close()

    replay = client.post("/tickets", json=VALID_REQUEST, headers=headers)
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()
    assert len(client.get("/tickets").json()) == 1


def test_legacy_idempotency_record_without_fingerprint_returns_409(client, tmp_path):
    """Scenario 4: an IdempotencyRecord created before payload binding has no
    request_hash field in its response_json.  Replaying such a key with *any*
    payload must fail safely with 409 rather than crashing or returning a stale
    response.

    We simulate the legacy record by inserting it directly into the DB before
    making the API call.
    """
    import json

    from sqlalchemy import text

    from src import db as _db

    # Ensure the tables exist (the fixture's TestClient already opened a session
    # that created them, but calling use_database reset the flag).
    _db.use_database(f"sqlite:///{tmp_path}/test.db")
    # We need a ticket row for the FK-like reference in IdempotencyRecord.
    first = client.post("/tickets", json=VALID_REQUEST)
    assert first.status_code == 201
    ticket_id = first.json()["ticket_id"]

    # Before payload binding, response_json stored the Ticket object directly,
    # with no request_hash field.
    legacy_response = json.dumps(first.json(), separators=(",", ":"))
    with _db.engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO idempotency_keys (key, ticket_id, response_json, created_at) "
                "VALUES (:key, :tid, :rj, datetime('now'))"
            ),
            {"key": "legacy-key-no-hash", "tid": ticket_id, "rj": legacy_response},
        )
        conn.commit()

    # Any replay of that legacy key — even with the same payload — must return
    # 409 because the stored record has no fingerprint to verify against.
    response = client.post(
        "/tickets", json=VALID_REQUEST, headers={"Idempotency-Key": "legacy-key-no-hash"}
    )
    assert response.status_code == 409


def test_stale_owner_cannot_finalize_after_takeover(client, monkeypatch):
    """A request whose lease expired cannot complete the new owner's reservation.

    Request A claims the key and pauses before the ticket pipeline runs. Its
    lease is moved into the past, request B reclaims the key and completes the
    ticket, then A resumes. Only B's fence token may finalize; A's ticket and
    execution-log writes must roll back together.
    """
    _ensure_tables()
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
    key = "takeover-finalize-key"
    headers = {"Idempotency-Key": key}

    with ThreadPoolExecutor(max_workers=1) as executor:
        stale_future = executor.submit(
            client.post, "/tickets", json=VALID_REQUEST, headers=headers
        )
        assert a_entered.wait(timeout=5), "stale owner never claimed the reservation"

        claimed = _reservation(key)
        assert claimed["payload"]["state"] == "processing"
        assert claimed["owner_token"]
        _make_reservation_stale(key)

        new_owner = client.post("/tickets", json=VALID_REQUEST, headers=headers)
        assert new_owner.status_code == 201
        after_takeover = _reservation(key)
        assert after_takeover["payload"]["state"] == "completed"
        # The takeover rotated the fencing token atomically.
        assert after_takeover["owner_token"] != claimed["owner_token"]

        release_a.set()
        stale_response = stale_future.result(timeout=5)

    assert stale_response.status_code == 409
    assert "took over" in stale_response.json()["detail"]

    # A's late ticket update and execution logs were rolled back with its failed
    # finalize; only B's durable effects remain.
    assert len(client.get("/tickets").json()) == 1
    actions = [log["action"] for log in client.get("/execution-logs").json()]
    assert actions.count("ticket.created") == 1
    assert "ticket.updated" not in actions

    # The reservation still belongs to B and replays B's response.
    final = _reservation(key)
    assert final["payload"]["state"] == "completed"
    assert final["owner_token"] == after_takeover["owner_token"]
    replay = client.post("/tickets", json=VALID_REQUEST, headers=headers)
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == new_owner.json()


def test_stale_owner_cleanup_cannot_delete_new_owner_reservation(client, monkeypatch):
    """Cleanup is fenced too: a stale owner cannot remove a live takeover.

    B reclaims the key but pauses while still processing. A resumes, fails in
    the pipeline, and runs its error cleanup. The conditional delete must leave
    B's processing reservation untouched so B can finish and serve replays.
    """
    _ensure_tables()
    a_entered = threading.Event()
    a_release = threading.Event()
    b_entered = threading.Event()
    b_release = threading.Event()
    calls = 0
    call_lock = threading.Lock()
    original_process = api._process_intake

    def controlled_process(request, session, actor_subject=None):
        nonlocal calls
        with call_lock:
            calls += 1
            call_number = calls
        if call_number == 1:
            a_entered.set()
            assert a_release.wait(timeout=5), "test did not release the stale owner"
            raise RuntimeError("stale owner failed after takeover")
        if call_number == 2:
            b_entered.set()
            assert b_release.wait(timeout=5), "test did not release the new owner"
        return original_process(request, session, actor_subject)

    monkeypatch.setattr(api, "_process_intake", controlled_process)
    key = "takeover-cleanup-key"
    headers = {"Idempotency-Key": key}

    with ThreadPoolExecutor(max_workers=2) as executor:
        stale_future = executor.submit(
            client.post, "/tickets", json=VALID_REQUEST, headers=headers
        )
        assert a_entered.wait(timeout=5), "stale owner never claimed the reservation"
        stale_token = _reservation(key)["owner_token"]
        _make_reservation_stale(key)

        new_future = executor.submit(
            client.post, "/tickets", json=VALID_REQUEST, headers=headers
        )
        assert b_entered.wait(timeout=5), "new owner never reclaimed the reservation"
        live = _reservation(key)
        assert live["payload"]["state"] == "processing"
        assert live["owner_token"] != stale_token

        a_release.set()
        with pytest.raises(RuntimeError, match="stale owner failed"):
            stale_future.result(timeout=5)

        # A's cleanup must not delete B's reservation.
        still_live = _reservation(key)
        assert still_live is not None
        assert still_live["payload"]["state"] == "processing"
        assert still_live["owner_token"] == live["owner_token"]

        b_release.set()
        new_owner = new_future.result(timeout=5)

    assert new_owner.status_code == 201
    completed = _reservation(key)
    assert completed["payload"]["state"] == "completed"
    assert completed["owner_token"] == live["owner_token"]

    replay = client.post("/tickets", json=VALID_REQUEST, headers=headers)
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == new_owner.json()
    assert len(client.get("/tickets").json()) == 1


def test_processing_failure_cleans_up_only_its_own_reservation(client, monkeypatch):
    """An ordinary failure releases its own key and leaves other keys untouched."""
    _ensure_tables()
    session = db.SessionLocal()
    try:
        session.add(
            db.IdempotencyRecord(
                key="foreign-in-flight-key",
                ticket_id="77",
                response_json=json.dumps(
                    {"request_hash": _request_hash(), "state": "processing"},
                    separators=(",", ":"),
                ),
                owner_token="foreign-owner-token",
                created_at=db.utcnow(),
            )
        )
        session.commit()
    finally:
        session.close()

    def fail_pipeline(request, session, actor_subject=None):
        raise RuntimeError("ticket pipeline exploded")

    monkeypatch.setattr(api, "_process_intake", fail_pipeline)
    with pytest.raises(RuntimeError, match="ticket pipeline exploded"):
        client.post(
            "/tickets",
            json=VALID_REQUEST,
            headers={"Idempotency-Key": "failing-key"},
        )

    assert _reservation("failing-key") is None
    foreign = _reservation("foreign-in-flight-key")
    assert foreign is not None
    assert foreign["payload"]["state"] == "processing"
    assert foreign["owner_token"] == "foreign-owner-token"


def test_existing_idempotency_table_gains_fencing_column(tmp_path):
    """A pre-fencing local database is upgraded additively, not recreated."""
    from sqlalchemy import inspect, text

    db.use_database(f"sqlite:///{tmp_path}/legacy.db")
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE idempotency_keys ("
                "key VARCHAR(128) PRIMARY KEY, "
                "ticket_id VARCHAR(64) NOT NULL, "
                "response_json TEXT NOT NULL, "
                "created_at DATETIME)"
            )
        )

    db._ensure_tables()

    columns = {
        column["name"]
        for column in inspect(db.engine).get_columns("idempotency_keys")
    }
    assert "owner_token" in columns
    assert "resource_type" in columns


# --- Webhook intake (v0.4) ---


def test_webhook_intake_accepts_single_ticket(client):
    response = client.post("/webhooks/intake", json={**VALID_REQUEST, "id": 50})
    assert response.status_code == 200
    assert response.json()["ticket_id"] == "50"


def test_webhook_intake_accepts_bulk_list(client):
    response = client.post(
        "/webhooks/intake",
        json=[
            {**VALID_REQUEST, "id": 51},
            {**VALID_REQUEST, "id": 52, "completed": True},
        ],
    )
    assert response.status_code == 200
    assert [ticket["ticket_id"] for ticket in response.json()] == ["51", "52"]
    ids = [ticket["ticket_id"] for ticket in client.get("/tickets").json()]
    assert ids == ["51", "52"]


def test_webhook_intake_rejects_invalid_item(client):
    response = client.post(
        "/webhooks/intake", json=[VALID_REQUEST, {**VALID_REQUEST, "id": "no"}]
    )
    assert response.status_code == 422
    assert client.get("/tickets").json() == []  # nothing stored on 422


# --- Execution logs (v0.3) ---


def test_execution_logs_recorded_for_intake(client):
    client.post("/tickets", json=VALID_REQUEST)
    logs = client.get("/execution-logs").json()
    assert any(
        log["action"] == "ticket.created" and log["status"] == "success" for log in logs
    )
    assert all(log["run_id"] for log in logs)


def test_execution_logs_filter_by_status(client):
    client.post("/tickets", json=VALID_REQUEST)
    assert client.get("/execution-logs", params={"status": "failure"}).json() == []
    successes = client.get("/execution-logs", params={"status": "success"}).json()
    assert successes and all(log["status"] == "success" for log in successes)


def test_notification_failure_is_logged_not_raised(client, monkeypatch):
    calls = []

    def fake_deliver(url, payload, **kwargs):
        calls.append(url)
        from src.notify import DeliveryResult

        return DeliveryResult(success=False, attempts=3, last_error="boom")

    monkeypatch.setattr(api, "deliver_webhook", fake_deliver)
    monkeypatch.setenv("FLOWOPS_NOTIFICATION_WEBHOOK_URL", "https://hooks.example/t")
    response = client.post("/tickets", json=VALID_REQUEST)
    assert response.status_code == 201
    assert calls == ["https://hooks.example/t"]
    failures = client.get("/execution-logs", params={"status": "failure"}).json()
    assert any(log["action"] == "notification.webhook" for log in failures)


# --- Notification outbox: failed webhooks stay visible and retryable ---


class FakeDelivery:
    """Scripted deliver_webhook replacement; records every call (offline)."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, url, payload, **kwargs):
        from src.notify import DeliveryResult

        self.calls.append((url, payload))
        if not self.results:  # fall back to the last scripted outcome
            result = self._last
        else:
            result = self.results.pop(0)
        self._last = result
        return DeliveryResult(**result)


def _failing_delivery(attempts=3, error="boom"):
    return {"success": False, "attempts": attempts, "last_error": error}


def _succeeding_delivery(attempts=1):
    return {"success": True, "attempts": attempts, "last_error": None}


def test_failed_webhook_is_stored_in_outbox(client, monkeypatch):
    fake = FakeDelivery([_failing_delivery()])
    monkeypatch.setattr(api, "deliver_webhook", fake)
    monkeypatch.setenv("FLOWOPS_NOTIFICATION_WEBHOOK_URL", "https://hooks.example/t")
    assert client.post("/tickets", json=VALID_REQUEST).status_code == 201

    pending = client.get("/notifications/pending").json()
    assert len(pending) == 1
    entry = pending[0]
    assert entry["status"] == "pending"
    assert entry["ticket_id"] == "42"
    assert entry["attempts"] == 3
    assert entry["last_error"] == "boom"


def test_successful_webhook_does_not_touch_outbox(client, monkeypatch):
    fake = FakeDelivery([_succeeding_delivery()])
    monkeypatch.setattr(api, "deliver_webhook", fake)
    monkeypatch.setenv("FLOWOPS_NOTIFICATION_WEBHOOK_URL", "https://hooks.example/t")
    assert client.post("/tickets", json=VALID_REQUEST).status_code == 201
    assert client.get("/notifications/pending").json() == []


def test_retry_endpoint_redelivers_pending_and_marks_delivered(client, monkeypatch):
    fake = FakeDelivery([_failing_delivery(), _succeeding_delivery(attempts=1)])
    monkeypatch.setattr(api, "deliver_webhook", fake)
    monkeypatch.setenv("FLOWOPS_NOTIFICATION_WEBHOOK_URL", "https://hooks.example/t")
    client.post("/tickets", json=VALID_REQUEST)

    retried = client.post("/notifications/retry").json()
    assert [entry["status"] for entry in retried] == ["delivered"]
    assert retried[0]["attempts"] == 4  # 3 original + 1 retry attempt
    assert retried[0]["last_error"] is None
    # The retry re-sent the same payload that originally failed.
    assert fake.calls[-1][1] == {
        "event": "ticket.created",
        "ticket": {
            "ticket_id": "42", "requester_id": 7, "title": "Laptop will not start",
            "status": "open", "priority": "normal", "category": "hardware",
            "queue": "it-hardware",
        },
    }
    assert client.get("/notifications/pending").json() == []
    logs = client.get("/execution-logs").json()
    assert any(
        log["action"] == "notification.retry" and log["status"] == "success"
        for log in logs
    )


def test_retry_keeps_still_failing_delivery_pending(client, monkeypatch):
    fake = FakeDelivery([_failing_delivery(), _failing_delivery(attempts=1)])
    monkeypatch.setattr(api, "deliver_webhook", fake)
    monkeypatch.setenv("FLOWOPS_NOTIFICATION_WEBHOOK_URL", "https://hooks.example/t")
    client.post("/tickets", json=VALID_REQUEST)

    retried = client.post("/notifications/retry").json()
    assert retried[0]["status"] == "pending"
    assert retried[0]["attempts"] == 4
    assert retried[0]["last_error"] == "boom"
    assert len(client.get("/notifications/pending").json()) == 1
    logs = client.get("/execution-logs").json()
    assert any(
        log["action"] == "notification.retry" and log["status"] == "failure"
        for log in logs
    )


def test_retry_dead_letters_delivery_at_attempt_limit(client, monkeypatch):
    fake = FakeDelivery([_failing_delivery(), _failing_delivery(attempts=1)])
    monkeypatch.setattr(api, "deliver_webhook", fake)
    monkeypatch.setenv("FLOWOPS_NOTIFICATION_WEBHOOK_URL", "https://hooks.example/t")
    monkeypatch.setenv("FLOWOPS_NOTIFICATION_MAX_ATTEMPTS", "4")
    client.post("/tickets", json=VALID_REQUEST)

    retried = client.post("/notifications/retry").json()
    assert [entry["status"] for entry in retried] == ["failed"]
    assert retried[0]["attempts"] == 4
    assert client.get("/notifications/pending").json() == []
    failed = client.get("/notifications/failed").json()
    assert len(failed) == 1
    assert failed[0]["id"] == retried[0]["id"]
    logs = client.get("/execution-logs").json()
    assert any(log["action"] == "notification.dead_lettered" for log in logs)

    # A dead-letter row is not retried again until an operator requeues it.
    calls_before = len(fake.calls)
    assert client.post("/notifications/retry").json() == []
    assert len(fake.calls) == calls_before


def test_requeue_failed_delivery_and_retry_succeeds(client, monkeypatch):
    fake = FakeDelivery(
        [
            _failing_delivery(),
            _failing_delivery(attempts=1),
            _succeeding_delivery(attempts=1),
        ]
    )
    monkeypatch.setattr(api, "deliver_webhook", fake)
    monkeypatch.setenv("FLOWOPS_NOTIFICATION_WEBHOOK_URL", "https://hooks.example/t")
    monkeypatch.setenv("FLOWOPS_NOTIFICATION_MAX_ATTEMPTS", "4")
    client.post("/tickets", json=VALID_REQUEST)
    client.post("/notifications/retry")  # 3 + 1 attempts -> dead-letter

    failed = client.get("/notifications/failed").json()
    assert len(failed) == 1
    requeued = client.post(f"/notifications/{failed[0]['id']}/requeue")
    assert requeued.status_code == 200
    assert requeued.json()["status"] == "pending"
    assert requeued.json()["attempts"] == 4
    assert requeued.json()["last_error"] == "boom"
    assert client.get("/notifications/failed").json() == []

    retried = client.post("/notifications/retry").json()
    assert [entry["status"] for entry in retried] == ["delivered"]
    assert retried[0]["attempts"] == 5
    assert retried[0]["last_error"] is None
    assert client.get("/notifications/pending").json() == []
    logs = client.get("/execution-logs").json()
    assert any(log["action"] == "notification.requeued" for log in logs)


def test_requeue_rejects_non_failed_or_unknown_delivery(client, monkeypatch):
    fake = FakeDelivery([_failing_delivery()])
    monkeypatch.setattr(api, "deliver_webhook", fake)
    monkeypatch.setenv("FLOWOPS_NOTIFICATION_WEBHOOK_URL", "https://hooks.example/t")
    client.post("/tickets", json=VALID_REQUEST)

    pending = client.get("/notifications/pending").json()[0]
    response = client.post(f"/notifications/{pending['id']}/requeue")
    assert response.status_code == 409
    assert "Only failed deliveries" in response.json()["detail"]
    assert client.post("/notifications/999/requeue").status_code == 404


def test_requeue_failed_delivery_only_once(client, monkeypatch):
    fake = FakeDelivery([_failing_delivery(), _failing_delivery(attempts=1)])
    monkeypatch.setattr(api, "deliver_webhook", fake)
    monkeypatch.setenv("FLOWOPS_NOTIFICATION_WEBHOOK_URL", "https://hooks.example/t")
    monkeypatch.setenv("FLOWOPS_NOTIFICATION_MAX_ATTEMPTS", "4")
    client.post("/tickets", json=VALID_REQUEST)
    client.post("/notifications/retry")

    failed = client.get("/notifications/failed").json()[0]
    assert client.post(f"/notifications/{failed['id']}/requeue").status_code == 200
    assert client.post(f"/notifications/{failed['id']}/requeue").status_code == 409


def test_retry_with_empty_queue_is_a_noop(client, monkeypatch):
    fake = FakeDelivery([])
    monkeypatch.setattr(api, "deliver_webhook", fake)
    assert client.post("/notifications/retry").json() == []
    assert fake.calls == []


def test_dashboard_shows_pending_retries_section(client, monkeypatch):
    fake = FakeDelivery([_failing_delivery()])
    monkeypatch.setattr(api, "deliver_webhook", fake)
    monkeypatch.setenv("FLOWOPS_NOTIFICATION_WEBHOOK_URL", "https://hooks.example/t")
    client.post("/tickets", json=VALID_REQUEST)

    page = client.get("/dashboard").text
    assert "Pending webhook deliveries" in page
    assert "Pending notifications" in page
    assert "hooks.example" in page  # the failing URL is surfaced


def test_dashboard_shows_dead_letter_section(client, monkeypatch):
    fake = FakeDelivery([_failing_delivery(), _failing_delivery(attempts=1)])
    monkeypatch.setattr(api, "deliver_webhook", fake)
    monkeypatch.setenv("FLOWOPS_NOTIFICATION_WEBHOOK_URL", "https://hooks.example/t")
    monkeypatch.setenv("FLOWOPS_NOTIFICATION_MAX_ATTEMPTS", "4")
    client.post("/tickets", json=VALID_REQUEST)
    client.post("/notifications/retry")

    page = client.get("/dashboard").text
    assert "Dead-letter notifications" in page
    assert "requeue" in page
    assert "hooks.example" in page


# --- Dashboard (v1.0 preview) ---


def test_dashboard_renders_stats(client):
    client.post("/tickets", json=VALID_REQUEST)
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "FlowOps monitoring dashboard" in response.text
    assert "ticket.created" in response.text
    page = response.text.lower()
    assert "pending notifications" in page
    assert "delivered notifications" in page
    assert "failed notifications" in page
    assert "notification retries" in page
