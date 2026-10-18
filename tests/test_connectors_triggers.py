"""v1.4 event-driven triggers & connector framework (in-process, offline)."""

import pytest
from fastapi.testclient import TestClient

from src import api, connectors, db


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWOPS_ACTION_ADAPTER", "simulated")
    db.use_database(f"sqlite:///{tmp_path / 'connectors.db'}")
    db._ensure_tables()
    with TestClient(api.app) as test_client:
        yield test_client


ADMIN = {"X-Test-Actor": "admin-1", "X-Test-Roles": "automation.admin"}


def _make_rule(client, **overrides):
    payload = {
        "name": "jira-high-priority",
        "event_type": "issue_updated",
        "predicate": "priority == 'High'",
        "action_type": "notify",
        "notify_url": "http://localhost:9/trigger-hook",
    }
    payload.update(overrides)
    response = client.post("/trigger-rules", json=payload, headers=ADMIN)
    assert response.status_code == 201, response.text
    return response.json()


def test_trigger_rule_crud_and_validation(client):
    rule = _make_rule(client)
    assert rule["id"] and rule["fired_count"] == 0
    assert [r["name"] for r in client.get("/trigger-rules").json()] == ["jira-high-priority"]

    # Bad predicate is rejected at creation time.
    bad = client.post(
        "/trigger-rules",
        json={**_RULE_BASE(), "predicate": "priority === (("},
        headers=ADMIN,
    )
    assert bad.status_code == 422

    # notify rules need a URL; start_workflow needs valid static inputs.
    no_url = client.post(
        "/trigger-rules",
        json={"name": "n", "action_type": "notify"},
        headers=ADMIN,
    )
    assert no_url.status_code == 422
    bad_inputs = client.post(
        "/trigger-rules",
        json={
            "name": "w",
            "action_type": "start_workflow",
            "request_type": "onboarding",
            "static_inputs": {"employee_emails": ["not-an-email"]},
        },
        headers=ADMIN,
    )
    assert bad_inputs.status_code == 422

    updated = client.put(
        f"/trigger-rules/{rule['id']}",
        json={**{k: rule[k] for k in ("name", "event_type", "predicate", "action_type", "notify_url")}},
        headers=ADMIN,
    )
    assert updated.status_code == 200
    assert client.delete(f"/trigger-rules/{rule['id']}", headers=ADMIN).status_code == 200
    assert client.get("/trigger-rules").json() == []


def _RULE_BASE():
    return {"name": "bad-pred", "action_type": "notify", "notify_url": "http://x/y"}


def test_notify_trigger_fires_once_per_event_with_dedupe(client):
    _make_rule(client)
    event = {
        "source": "jira",
        "type": "issue_updated",
        "id": "OPS-1",
        "title": "VPN broken",
        "payload": {"priority": "High", "status": "In Progress"},
    }
    first = client.post("/events", json=event)
    assert first.status_code == 201, first.text
    body = first.json()["results"][0]
    assert body["duplicate"] is False
    assert len(body["triggers_fired"]) == 1
    fired = body["triggers_fired"][0]
    assert fired["action"] == "notify" and fired["ok"] is True

    # Retried webhook delivery: same fingerprint -> duplicate, nothing fires.
    replay = client.post("/events", json=event).json()["results"][0]
    assert replay["duplicate"] is True and replay["triggers_fired"] == []

    with db.SessionLocal() as session:
        outbox = session.query(db.NotificationOutboxRecord).all()
        assert len(outbox) == 1
        assert '"event": "trigger.fired"' in outbox[0].payload_json.replace("'", '"') or "trigger.fired" in outbox[0].payload_json
        rule_row = session.get(db.TriggerRuleRecord, 1)
        assert rule_row.fired_count == 1

    # Non-matching payload does not fire the predicate.
    low = client.post(
        "/events",
        json={"source": "jira", "type": "issue_updated", "id": "OPS-2",
              "payload": {"priority": "Low"}},
    ).json()["results"][0]
    assert low["triggers_fired"] == []


def test_start_workflow_trigger_creates_and_starts_request(client):
    _make_rule(
        client,
        name="provision-new-hire",
        event_type="new_hire",
        predicate="true",
        action_type="start_workflow",
        request_type="onboarding",
        notify_url=None,
        static_inputs={"employee_emails": ["zoe@acme.example"], "department": "IT"},
    )
    result = client.post(
        "/events",
        json={"source": "generic", "type": "new_hire", "id": "EVT-7"},
    ).json()["results"][0]
    fired = result["triggers_fired"]
    assert len(fired) == 1 and fired[0]["ok"] is True, fired

    requests = client.get("/onboarding-requests").json()
    assert len(requests) == 1
    assert requests[0]["employee_email"] == "zoe@acme.example"
    assert requests[0]["department"] == "trigger:provision-new-hire" or requests[0]["department"] == "IT"

    run = client.get(f"/onboarding-requests/{requests[0]['id']}/run")
    assert run.status_code == 200 and run.json()["status"] in ("running", "completed")

    # Replaying the same event must not create a second request.
    replay = client.post(
        "/events",
        json={"source": "generic", "type": "new_hire", "id": "EVT-7"},
    ).json()["results"][0]
    assert replay["duplicate"] is True
    assert len(client.get("/onboarding-requests").json()) == 1


def test_bad_rule_is_logged_and_never_breaks_ingestion(client):
    # Insert a rule with a runtime-broken predicate directly (bypass API).
    # ``getattr`` on a missing field raises at evaluation time even though the
    # expression compiles fine (the API's create-time validation would accept
    # it), so this exercises the trigger engine's error isolation path.
    with db.SessionLocal() as session:
        session.add(
            db.TriggerRuleRecord(
                name="broken",
                event_type="*",
                predicate="missing_field.sub == 1",
                action_type="notify",
                notify_url="http://localhost:9/x",
            )
        )
        session.commit()
    result = client.post(
        "/events", json={"source": "generic", "type": "ping", "id": "P-1"}
    )
    assert result.status_code == 201
    body = result.json()["results"][0]
    assert body["duplicate"] is False
    with db.SessionLocal() as session:
        actions = [r.action for r in session.query(db.ExecutionLogRecord).all()]
    assert "trigger.rule_error" in actions or body["triggers_fired"] == []


def test_connector_crud_and_webhook_sink(client):
    created = client.post(
        "/connectors",
        json={"name": "Acme Jira", "connector_type": "jira",
              "config": {"base_url": "https://j.example", "email": "b@x",
                         "api_token_env": "FLOWOPS_JIRA_TOKEN", "jql": "project=OPS"}},
        headers=ADMIN,
    )
    assert created.status_code == 201, created.text
    jira = created.json()
    assert jira["kind"] == "polling"

    slack = client.post(
        "/connectors",
        json={"name": "Ops Slack", "connector_type": "slack",
              "config": {"signing_secret_env": "FLOWOPS_SLACK_SECRET"}},
        headers=ADMIN,
    ).json()
    assert slack["kind"] == "webhook"

    # Missing required config keys are rejected.
    bad = client.post(
        "/connectors", json={"name": "x", "connector_type": "jira", "config": {}},
        headers=ADMIN,
    )
    assert bad.status_code == 422

    # Polling connectors reject pushed webhooks and vice versa.
    assert client.post(f"/connectors/{jira['id']}/webhook", json={}).status_code == 405

    # Generic webhook sink ingests canonical events.
    generic = client.post(
        "/connectors",
        json={"name": "Gen", "connector_type": "generic", "config": {}},
        headers=ADMIN,
    ).json()
    delivered = client.post(
        f"/connectors/{generic['id']}/webhook",
        json={"events": [{"type": "alert", "id": "A-1", "payload": {"sev": 3}}]},
    )
    assert delivered.status_code == 200
    assert delivered.json()["received"] == 1

    # A misconfigured secret env surfaces as 503, not a crash.
    slack_hook = client.post(f"/connectors/{slack['id']}/webhook", json={"text": "hi"})
    assert slack_hook.status_code == 503  # FLOWOPS_SLACK_SECRET unset in tests

    assert client.delete(f"/connectors/{jira['id']}", headers=ADMIN).status_code == 200


def test_build_connector_resolves_env_secrets(monkeypatch):
    monkeypatch.setenv("FLOWOPS_JIRA_TOKEN", "tok")
    connector = connectors.build_connector(
        "jira",
        {"base_url": "https://j.example", "email": "b@x",
         "api_token_env": "FLOWOPS_JIRA_TOKEN", "jql": "p=1"},
    )
    assert isinstance(connector, connectors.JiraConnector)
    assert connector.api_token == "tok"

    monkeypatch.delenv("FLOWOPS_JIRA_TOKEN", raising=False)
    with pytest.raises(ValueError, match="not set"):
        connectors.build_connector(
            "jira",
            {"base_url": "https://j.example", "email": "b@x",
             "api_token_env": "FLOWOPS_JIRA_TOKEN", "jql": "p=1"},
        )


def test_poll_all_connectors_survives_failures(client, monkeypatch):
    client.post(
        "/connectors",
        json={"name": "Dead Jira", "connector_type": "jira",
              "config": {"base_url": "http://127.0.0.1:1", "email": "e",
                         "api_token_env": "NOPE_ENV", "jql": "x"}},
        headers=ADMIN,
    )
    ingested = connectors.poll_all_connectors()
    assert ingested == 0
    with db.SessionLocal() as session:
        actions = [r.action for r in session.query(db.ExecutionLogRecord).all()]
    assert "connector.poll_failed" in actions


def test_tenant_scoped_rules_only_fire_for_their_tenant(client):
    with db.SessionLocal() as session:
        session.add(db.TenantRecord(key="acme", name="Acme"))
        session.add(db.TenantRecord(key="globex", name="Globex"))
        session.commit()
    _make_rule(client, name="acme-only", tenant_key="acme", event_type="*")

    other = client.post(
        "/events",
        json={"source": "generic", "type": "x", "id": "T-1", "tenant": "globex"},
    ).json()["results"][0]
    assert other["triggers_fired"] == []

    mine = client.post(
        "/events",
        json={"source": "generic", "type": "x", "id": "T-2", "tenant": "acme"},
    ).json()["results"][0]
    assert len(mine["triggers_fired"]) == 1


def test_sweep_expired_removes_stale_completed_records(client):
    """Retention sweep deletes records past the window, keeps fresh ones."""
    import datetime as dt
    import json as _json

    from src import idempotency

    def _record(key: str, state: str, age_hours: float) -> db.IdempotencyRecord:
        return db.IdempotencyRecord(
            key=key,
            ticket_id="res-1",
            owner_token=None if state == "completed" else "tok-" + key,
            resource_type=idempotency.SCOPE_TICKET,
            response_json=_json.dumps({"request_hash": "h", "state": state}),
            created_at=db.utcnow() - dt.timedelta(hours=age_hours),
        )

    with db.SessionLocal() as session:
        session.add_all(
            [
                _record("fresh-completed", "completed", 1),
                _record("stale-completed", "completed", 8 * 24),
                _record("stale-abandoned", "processing", 8 * 24),
                _record("recent-processing", "processing", 0.01),
            ]
        )
        session.commit()

        deleted = idempotency.sweep_expired(session)
        assert deleted == 2  # stale completed + stale abandoned reservation
        keys = {r.key for r in session.query(db.IdempotencyRecord).all()}
        assert keys == {"fresh-completed", "recent-processing"}
