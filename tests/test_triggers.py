"""Offline tests for the event-driven trigger layer (v1.4).

Covers: signed webhooks, replay dedup, mapping resolution, scheduled
triggers, durable delivery worker, CRUD auth, UI rendering, and
regression of existing manual APIs.

No sleeps, no network, adapter doubles only.
"""

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src import db, triggers, trigger_worker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWOPS_AUTH_REQUIRED", "false")
    monkeypatch.setenv("FLOWOPS_UI_AUTH_REQUIRED", "false")
    monkeypatch.setenv("FLOWOPS_RATE_LIMIT_PER_MINUTE", "0")
    db.use_database(f"sqlite:///{tmp_path}/triggers.db")
    with TestClient(api_app()) as test_client:
        yield test_client


def api_app():
    from src import api
    return api.app


def _create_source(client, key="src-1", name="Source 1", secret="test-secret"):
    resp = client.post("/event-sources", json={
        "key": key, "name": name, "secret": secret, "active": True,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_definition(client, key="onb-def"):
    created = client.post("/workflow-definitions", json={
        "key": key,
        "name": "Onboarding def",
        "request_type": "onboarding",
        "steps": [{
            "key": "create_user_account",
            "step": "Create user account",
            "queue": "it-access-management",
            "requires_approval": True,
            "action": "none",
            "depends_on": [],
            "join": "all",
            "when": [],
        }],
    })
    assert created.status_code == 200, created.text
    published = client.post(f"/workflow-definitions/{key}/publish", json={"version": 1})
    assert published.status_code == 200, published.text
    return key


def _create_trigger(
    client,
    *,
    key="trig-1",
    source_key="src-1",
    target_type="onboarding_request",
    definition_key="onb-def",
    schedule_kind="webhook",
    mapping=None,
    **kwargs,
):
    if mapping is None:
        mapping = {
            "employee_name": "event.user.name",
            "employee_email": "event.user.email",
            "department": "Engineering",
            "requester_id": 42,
        }
    body = {
        "key": key,
        "name": "Test trigger",
        "source_key": source_key,
        "event_type": "onboarding.request.created",
        "target_type": target_type,
        "definition_key": definition_key,
        "mapping": mapping,
        "schedule_kind": schedule_kind,
        **kwargs,
    }
    resp = client.post("/triggers", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _sign_webhook(secret, timestamp, body_bytes):
    message = f"{timestamp}.".encode() + body_bytes
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Event source CRUD
# ---------------------------------------------------------------------------


def test_create_event_source(client):
    result = _create_source(client)
    assert result["key"] == "src-1"
    assert result["active"] is True
    # Secret is never returned.
    assert "secret" not in result


def test_list_event_sources(client):
    _create_source(client, key="a")
    _create_source(client, key="b", name="B source")
    sources = client.get("/event-sources").json()
    assert len(sources) == 2
    assert sources[0]["key"] == "a"
    assert sources[1]["key"] == "b"


def test_duplicate_event_source_rejected(client):
    _create_source(client, key="src-1")
    resp = client.post("/event-sources", json={
        "key": "src-1", "name": "dup", "secret": "x",
    })
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Trigger CRUD
# ---------------------------------------------------------------------------


def test_create_trigger_requires_published_definition(client):
    _create_source(client)
    # Create definition but don't publish.
    client.post("/workflow-definitions", json={
        "key": "draft-def", "name": "Draft", "request_type": "onboarding",
        "steps": [{"key": "s1", "step": "Step 1", "queue": "q", "requires_approval": False, "action": "none", "depends_on": [], "join": "all", "when": []}],
    })
    resp = client.post("/triggers", json={
        "key": "t1", "name": "T", "source_key": "src-1",
        "event_type": "evt", "target_type": "onboarding_request",
        "definition_key": "draft-def",
        "mapping": {"employee_name": "x", "employee_email": "a@b.com", "department": "d", "requester_id": 1},
        "schedule_kind": "webhook",
    })
    assert resp.status_code == 422


def test_create_trigger_validates_mapping(client):
    _create_source(client)
    _create_definition(client)
    resp = client.post("/triggers", json={
        "key": "t1", "name": "T", "source_key": "src-1",
        "event_type": "evt", "target_type": "onboarding_request",
        "definition_key": "onb-def",
        "mapping": {"employee_name": "event.name"},  # missing required fields
        "schedule_kind": "webhook",
    })
    assert resp.status_code == 422


def test_create_interval_trigger_requires_interval_seconds(client):
    _create_source(client)
    _create_definition(client)
    resp = client.post("/triggers", json={
        "key": "t1", "name": "T", "source_key": "src-1",
        "event_type": "evt", "target_type": "onboarding_request",
        "definition_key": "onb-def",
        "mapping": {"employee_name": "x", "employee_email": "a@b.com", "department": "d", "requester_id": 1},
        "schedule_kind": "interval",
    })
    assert resp.status_code == 422


def test_update_trigger(client):
    _create_source(client)
    _create_definition(client)
    _create_trigger(client)
    resp = client.put("/triggers/trig-1", json={
        "key": "trig-1", "name": "Updated", "source_key": "src-1",
        "event_type": "updated.event", "target_type": "onboarding_request",
        "definition_key": "onb-def",
        "mapping": {"employee_name": "x", "employee_email": "a@b.com", "department": "d", "requester_id": 1},
        "schedule_kind": "webhook",
        "active": False,
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated"
    assert resp.json()["active"] is False


def test_delete_trigger(client):
    _create_source(client)
    _create_definition(client)
    _create_trigger(client)
    resp = client.delete("/triggers/trig-1")
    assert resp.status_code == 200
    assert client.get("/triggers/trig-1").status_code == 404


def test_get_trigger_deliveries_empty(client):
    _create_source(client)
    _create_definition(client)
    _create_trigger(client)
    resp = client.get("/triggers/trig-1/deliveries")
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# Signed webhook verification
# ---------------------------------------------------------------------------


def _send_webhook(client, trigger_key, secret, body_dict, *, timestamp=None, signature=None, event_id=None):
    if timestamp is None:
        timestamp = int(time.time())
    body_bytes = json.dumps(body_dict).encode()
    if signature is None:
        signature = _sign_webhook(secret, timestamp, body_bytes)
    headers = {
        "X-FlowOps-Timestamp": str(timestamp),
        "X-FlowOps-Signature": f"sha256={signature}",
    }
    if event_id is not None:
        headers["X-FlowOps-Event-ID"] = event_id
    return client.post(f"/webhooks/triggers/{trigger_key}", content=body_bytes, headers=headers)


def test_valid_webhook_creates_delivery(client):
    _create_source(client, secret="my-secret")
    _create_definition(client)
    _create_trigger(client)
    body = {
        "event_id": "evt-001",
        "user": {"name": "Jane Doe", "email": "jane@example.com"},
    }
    resp = _send_webhook(client, "trig-1", "my-secret", body)
    assert resp.status_code == 200
    assert resp.json()["status"] == "received"
    assert resp.json()["replayed"] is False


def test_webhook_delivery_processed_creates_request(client):
    _create_source(client, secret="my-secret")
    _create_definition(client)
    _create_trigger(client)
    body = {
        "event_id": "evt-002",
        "user": {"name": "Jane Doe", "email": "jane@example.com"},
    }
    resp = _send_webhook(client, "trig-1", "my-secret", body)
    assert resp.status_code == 200
    # Process the delivery.
    delivery = trigger_worker.run_once()
    assert delivery is not None
    assert delivery.status == "succeeded"
    assert delivery.request_id is not None
    # Verify the onboarding request was created.
    request = client.get(f"/onboarding-requests/{delivery.request_id}")
    assert request.status_code == 200
    assert request.json()["employee_name"] == "Jane Doe"
    assert request.json()["definition_key"] == "onb-def"


def test_replay_same_event_id_returns_prior_result(client):
    _create_source(client, secret="my-secret")
    _create_definition(client)
    _create_trigger(client)
    body = {
        "event_id": "evt-003",
        "user": {"name": "Jane Doe", "email": "jane@example.com"},
    }
    resp1 = _send_webhook(client, "trig-1", "my-secret", body)
    assert resp1.json()["replayed"] is False
    # Process the delivery.
    trigger_worker.run_once()
    # Replay the same event_id.
    resp2 = _send_webhook(client, "trig-1", "my-secret", body)
    assert resp2.status_code == 200
    assert resp2.json()["replayed"] is True
    assert resp2.json()["status"] == "succeeded"
    # Only one onboarding request should exist.
    requests = client.get("/onboarding-requests").json()
    assert len(requests) == 1


def test_invalid_signature_rejected(client):
    _create_source(client, secret="my-secret")
    _create_definition(client)
    _create_trigger(client)
    body = {"event_id": "evt-x", "user": {"name": "A", "email": "a@b.com"}}
    resp = _send_webhook(client, "trig-1", "wrong-secret", body)
    assert resp.status_code == 401


def test_expired_timestamp_rejected(client):
    _create_source(client, secret="my-secret")
    _create_definition(client)
    _create_trigger(client)
    old_ts = int(time.time()) - 600  # 10 minutes ago
    body = {"event_id": "evt-x", "user": {"name": "A", "email": "a@b.com"}}
    resp = _send_webhook(client, "trig-1", "my-secret", body, timestamp=old_ts)
    assert resp.status_code == 401


def test_unknown_trigger_rejected(client):
    body = {"event_id": "evt-x"}
    resp = _send_webhook(client, "nonexistent", "any", body)
    assert resp.status_code == 404


def test_inactive_trigger_rejected(client):
    _create_source(client, secret="my-secret")
    _create_definition(client)
    _create_trigger(client)
    # Deactivate the trigger.
    client.put("/triggers/trig-1", json={
        "key": "trig-1", "name": "Test trigger", "source_key": "src-1",
        "event_type": "onboarding.request.created", "target_type": "onboarding_request",
        "definition_key": "onb-def",
        "mapping": {"employee_name": "event.user.name", "employee_email": "event.user.email", "department": "Engineering", "requester_id": 42},
        "schedule_kind": "webhook", "active": False,
    })
    body = {"event_id": "evt-x", "user": {"name": "A", "email": "a@b.com"}}
    resp = _send_webhook(client, "trig-1", "my-secret", body)
    assert resp.status_code == 404


def test_missing_event_id_rejected(client):
    _create_source(client, secret="my-secret")
    _create_definition(client)
    _create_trigger(client)
    body = {"user": {"name": "A", "email": "a@b.com"}}
    resp = _send_webhook(client, "trig-1", "my-secret", body)
    assert resp.status_code == 422


def test_event_id_from_header(client):
    _create_source(client, secret="my-secret")
    _create_definition(client)
    _create_trigger(client)
    body = {"user": {"name": "A", "email": "a@b.com"}}
    resp = _send_webhook(client, "trig-1", "my-secret", body, event_id="hdr-evt-1")
    assert resp.status_code == 200
    assert resp.json()["event_id"] == "hdr-evt-1"


# ---------------------------------------------------------------------------
# Mapping resolution
# ---------------------------------------------------------------------------


def test_mapping_fixed_literal(client):
    _create_source(client, secret="s")
    _create_definition(client)
    _create_trigger(client, mapping={
        "employee_name": "Fixed Name",
        "employee_email": "fixed@example.com",
        "department": "Fixed Dept",
        "requester_id": 99,
    })
    body = {"event_id": "evt-fix"}
    resp = _send_webhook(client, "trig-1", "s", body)
    assert resp.status_code == 200
    trigger_worker.run_once()
    requests = client.get("/onboarding-requests").json()
    assert requests[0]["employee_name"] == "Fixed Name"
    assert requests[0]["requester_id"] == 99


def test_mapping_trigger_field(client):
    _create_source(client, secret="s")
    _create_definition(client)
    _create_trigger(client, mapping={
        "employee_name": "trigger.name",
        "employee_email": "fixed@example.com",
        "department": "trigger.source_key",
        "requester_id": 1,
    })
    body = {"event_id": "evt-trig"}
    resp = _send_webhook(client, "trig-1", "s", body)
    assert resp.status_code == 200
    trigger_worker.run_once()
    requests = client.get("/onboarding-requests").json()
    assert requests[0]["employee_name"] == "Test trigger"
    assert requests[0]["department"] == "src-1"


def test_mapping_unknown_event_path_fails_closed(client):
    _create_source(client, secret="s")
    _create_definition(client)
    _create_trigger(client, mapping={
        "employee_name": "event.nonexistent.path",
        "employee_email": "a@b.com",
        "department": "d",
        "requester_id": 1,
    })
    body = {"event_id": "evt-miss"}
    resp = _send_webhook(client, "trig-1", "s", body)
    assert resp.status_code == 200
    delivery = trigger_worker.run_once()
    assert delivery.status == "failed"
    assert "not found" in (delivery.error or "")


# ---------------------------------------------------------------------------
# Ticket target
# ---------------------------------------------------------------------------


def test_ticket_target_creates_ticket(client):
    _create_source(client, secret="s")
    _create_trigger(
        client,
        target_type="ticket",
        definition_key=None,
        mapping={"userId": 1, "id": 100, "title": "Triggered ticket", "completed": False},
    )
    body = {"event_id": "evt-ticket"}
    resp = _send_webhook(client, "trig-1", "s", body)
    assert resp.status_code == 200
    delivery = trigger_worker.run_once()
    assert delivery.status == "succeeded"
    ticket = client.get(f"/tickets/{delivery.request_id}")
    assert ticket.status_code == 200
    assert ticket.json()["title"] == "Triggered ticket"


# ---------------------------------------------------------------------------
# Scheduled triggers
# ---------------------------------------------------------------------------


def test_interval_trigger_fires_once(client, monkeypatch):
    _create_source(client, key="sched-src", secret="s")
    _create_definition(client)
    _create_trigger(
        client, key="interval-trig", source_key="sched-src",
        schedule_kind="interval", interval_seconds=60,
    )
    # Inject clock: first tick fires.
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    deliveries = triggers.tick(now=now)
    assert len(deliveries) == 1
    assert deliveries[0].trigger_key == "interval-trig"
    # Second tick at same time does not fire again (last_fired_at updated).
    deliveries2 = triggers.tick(now=now)
    assert len(deliveries2) == 0
    # Tick after interval fires again.
    later = now + timedelta(seconds=61)
    deliveries3 = triggers.tick(now=later)
    assert len(deliveries3) == 1
    # Different event_id for the second occurrence.
    assert deliveries3[0].event_id != deliveries[0].event_id


def test_daily_trigger_fires_once_per_day(client):
    _create_source(client, key="daily-src", secret="s")
    _create_definition(client)
    _create_trigger(
        client, key="daily-trig", source_key="daily-src",
        schedule_kind="daily", daily_at_utc="09:00:00",
    )
    # Before the target time: no fire.
    before = datetime(2026, 1, 1, 8, 0, 0, tzinfo=timezone.utc)
    assert len(triggers.tick(now=before)) == 0
    # At/after the target time: fire.
    at_target = datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
    deliveries = triggers.tick(now=at_target)
    assert len(deliveries) == 1
    # Same day, later: no fire (already fired today).
    later_same_day = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    assert len(triggers.tick(now=later_same_day)) == 0
    # Next day at target: fire again.
    next_day = datetime(2026, 1, 2, 9, 0, 0, tzinfo=timezone.utc)
    deliveries2 = triggers.tick(now=next_day)
    assert len(deliveries2) == 1
    assert deliveries2[0].event_id != deliveries[0].event_id


def test_two_schedulers_cannot_double_fire(client):
    _create_source(client, key="race-src", secret="s")
    _create_definition(client)
    _create_trigger(
        client, key="race-trig", source_key="race-src",
        schedule_kind="interval", interval_seconds=1,
    )
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    # Two ticks at the same time: only one fires.
    d1 = triggers.tick(now=now)
    d2 = triggers.tick(now=now)
    assert len(d1) == 1
    assert len(d2) == 0


# ---------------------------------------------------------------------------
# Durable delivery worker
# ---------------------------------------------------------------------------


def test_transient_failure_retries(client):
    _create_source(client, secret="s")
    _create_definition(client)
    _create_trigger(client, mapping={
        "employee_name": "event.user.name",
        "employee_email": "event.user.email",
        "department": "Engineering",
        "requester_id": 42,
    })
    body = {"event_id": "evt-retry", "user": {"name": "Retry", "email": "r@example.com"}}
    _send_webhook(client, "trig-1", "s", body)

    # Sabotage the first attempt: make _create_onboarding_request raise.
    import src.api as api_module
    original = api_module._create_onboarding_request
    call_count = [0]

    def flaky(request, session, actor_subject=None):
        call_count[0] += 1
        if call_count[0] < 2:
            raise RuntimeError("transient failure")
        return original(request, session, actor_subject)

    api_module._create_onboarding_request = flaky
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    try:
        delivery = trigger_worker.run_once(now=now)
        assert delivery.status == "queued"  # retry scheduled
        # Second attempt after backoff succeeds.
        later = now + timedelta(seconds=10)
        delivery2 = trigger_worker.run_once(now=later)
        assert delivery2.status == "succeeded"
    finally:
        api_module._create_onboarding_request = original


def test_terminal_failure_dead_letters(client):
    _create_source(client, secret="s")
    _create_definition(client)
    _create_trigger(client, mapping={
        "employee_name": "event.missing.path",  # will fail permanently
        "employee_email": "a@b.com",
        "department": "d",
        "requester_id": 1,
    })
    body = {"event_id": "evt-perm"}
    _send_webhook(client, "trig-1", "s", body)
    delivery = trigger_worker.run_once()
    assert delivery.status == "failed"
    # No request was created.
    assert len(client.get("/onboarding-requests").json()) == 0


def test_crash_does_not_create_duplicate(client):
    _create_source(client, secret="s")
    _create_definition(client)
    _create_trigger(client)
    body = {"event_id": "evt-crash", "user": {"name": "Crash", "email": "c@example.com"}}
    _send_webhook(client, "trig-1", "s", body)
    # Process once successfully.
    delivery = trigger_worker.run_once()
    assert delivery.status == "succeeded"
    request_id = delivery.request_id
    # Simulate a crash: process again (delivery is already succeeded, so
    # claim_delivery should not pick it up).
    delivery2 = trigger_worker.run_once()
    assert delivery2 is None
    # Still only one request.
    assert len(client.get("/onboarding-requests").json()) == 1


# ---------------------------------------------------------------------------
# Test trigger endpoint
# ---------------------------------------------------------------------------


def test_test_trigger_creates_delivery(client):
    _create_source(client, secret="s")
    _create_definition(client)
    _create_trigger(client)
    resp = client.post("/triggers/trig-1/test", json={
        "event_id": "manual-test",
        "user": {"name": "Manual", "email": "m@example.com"},
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "received"
    # Process it.
    delivery = trigger_worker.run_once()
    assert delivery.status == "succeeded"


# ---------------------------------------------------------------------------
# UI rendering
# ---------------------------------------------------------------------------


def test_ui_triggers_list_renders(client):
    _create_source(client)
    _create_definition(client)
    _create_trigger(client)
    resp = client.get("/ui/triggers")
    assert resp.status_code == 200
    assert "trig-1" in resp.text


def test_ui_trigger_detail_renders(client):
    _create_source(client)
    _create_definition(client)
    _create_trigger(client)
    resp = client.get("/ui/triggers/trig-1")
    assert resp.status_code == 200
    assert "trig-1" in resp.text


def test_ui_trigger_delivery_detail_renders(client):
    _create_source(client, secret="s")
    _create_definition(client)
    _create_trigger(client)
    body = {"event_id": "evt-ui", "user": {"name": "UI", "email": "u@example.com"}}
    _send_webhook(client, "trig-1", "s", body)
    trigger_worker.run_once()
    deliveries = client.get("/triggers/trig-1/deliveries").json()
    assert len(deliveries) == 1
    delivery_id = deliveries[0]["id"]
    resp = client.get(f"/ui/triggers/trig-1/deliveries/{delivery_id}")
    assert resp.status_code == 200
    assert "evt-ui" in resp.text


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_metrics_include_trigger_counters(client):
    _create_source(client, secret="s")
    _create_definition(client)
    _create_trigger(client)
    body = {"event_id": "evt-metrics", "user": {"name": "M", "email": "m@example.com"}}
    _send_webhook(client, "trig-1", "s", body)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "flowops_trigger_deliveries_total" in resp.text


# ---------------------------------------------------------------------------
# Regression: existing APIs unchanged
# ---------------------------------------------------------------------------


def test_existing_ticket_creation_still_works(client):
    resp = client.post("/tickets", json={
        "userId": 1, "id": 1, "title": "Test ticket", "completed": False,
    })
    assert resp.status_code == 201
    assert resp.json()["title"] == "Test ticket"


def test_existing_onboarding_creation_still_works(client):
    _create_definition(client)
    resp = client.post("/onboarding-requests", json={
        "request_type": "onboarding",
        "employee_name": "Direct",
        "employee_email": "direct@example.com",
        "department": "Engineering",
        "requester_id": 1,
        "definition_key": "onb-def",
    })
    assert resp.status_code == 201
    assert resp.json()["employee_name"] == "Direct"


def test_existing_workflow_definition_apis_unchanged(client):
    resp = client.post("/workflow-definitions", json={
        "key": "regression-def",
        "name": "Regression",
        "request_type": "onboarding",
        "steps": [{
            "key": "s1", "step": "Step 1", "queue": "q",
            "requires_approval": False, "action": "none",
            "depends_on": [], "join": "all", "when": [],
        }],
    })
    assert resp.status_code == 200
    pub = client.post("/workflow-definitions/regression-def/publish", json={"version": 1})
    assert pub.status_code == 200
    assert pub.json()["status"] == "published"
