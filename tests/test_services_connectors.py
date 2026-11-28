"""Characterization tests for the connector/trigger service surface.

These pin the behavior of ``src/services/connectors.py``: pure validation
errors, fingerprint stability, ingestion dedupe, rule scoping/idempotency,
poll backoff, and outbox delivery revalidation. Written against the
pre-extraction owner (``src.connectors``) and repointed with the move.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from src import db
from src.services import connectors


@pytest.fixture
def fresh_db(tmp_path):
    db.use_database(f"sqlite:///{tmp_path / 'svc-connectors.db'}")
    db._ensure_tables()


def _event(**overrides):
    defaults = dict(
        source="jira",
        event_type="issue_updated",
        external_id="ISSUE-1",
        payload={"key": "OPS-1"},
        tenant_key=None,
        title="Jira OPS-1",
        description="",
        connector_id=None,
    )
    defaults.update(overrides)
    return connectors.NormalizedEvent(**defaults)


def test_fingerprint_is_stable_and_scope_sensitive(fresh_db):
    first = _event().fingerprint()
    assert first == _event().fingerprint()
    assert first.startswith("v2:")
    assert first != _event(external_id="ISSUE-2").fingerprint()
    assert first != _event(tenant_key="tenant-a").fingerprint()


def test_trigger_context_envelope_fields_win_over_payload(fresh_db):
    event = _event(payload={"source": "spoofed", "custom": 1})
    context = event.trigger_context()
    assert context["source"] == "jira"  # envelope cannot be spoofed
    assert context["custom"] == 1  # payload keys still available


def test_validate_connector_config_requires_managed_env_reference(monkeypatch):
    monkeypatch.setenv("FLOWOPS_CONNECTOR_ALLOWED_HOSTS_JSON", '{"*": ["j.example"]}')
    config = {"base_url": "https://j.example", "email": "e", "api_token_env": "x", "jql": "x"}
    with pytest.raises(ValueError, match="server-managed"):
        connectors.validate_connector_config("jira", config)
    config["api_token_env"] = "FLOWOPS_CONNECTOR_JIRA_API_TOKEN"
    connectors.validate_connector_config("jira", config)  # no raise


def test_validate_connector_config_enforces_https_and_allowlist(monkeypatch):
    monkeypatch.setenv("FLOWOPS_CONNECTOR_ALLOWED_HOSTS_JSON", '{"*": ["j.example"]}')
    base = {"email": "e", "api_token_env": "FLOWOPS_CONNECTOR_JIRA_API_TOKEN", "jql": "x"}
    with pytest.raises(ValueError, match="HTTPS"):
        connectors.validate_connector_config("jira", {"base_url": "http://j.example", **base})
    with pytest.raises(ValueError, match="not approved"):
        connectors.validate_connector_config("jira", {"base_url": "https://other.example", **base})


def test_build_connector_resolves_env_secret_and_fails_closed(monkeypatch):
    monkeypatch.setenv("FLOWOPS_CONNECTOR_ALLOWED_HOSTS_JSON", '{"*": ["j.example"]}')
    monkeypatch.setenv("FLOWOPS_CONNECTOR_JIRA_API_TOKEN", "tok")
    config = {"base_url": "https://j.example", "email": "e",
              "api_token_env": "FLOWOPS_CONNECTOR_JIRA_API_TOKEN", "jql": "project = OPS"}
    connector = connectors.build_connector("jira", config)
    assert isinstance(connector, connectors.JiraConnector)
    assert connector.api_token == "tok"
    monkeypatch.delenv("FLOWOPS_CONNECTOR_JIRA_API_TOKEN")
    with pytest.raises(ValueError, match="not set"):
        connectors.build_connector("jira", config)


def test_ingest_event_dedupes_and_fires_notify_rule_once(fresh_db):
    session = db.SessionLocal()
    try:
        session.add(db.TriggerRuleRecord(
            name="notify-on-issue", source="", event_type="issue_updated",
            predicate="True", action_type="notify", notify_url="https://hooks.example/x",
            enabled=True,
        ))
        session.commit()
        first = connectors.ingest_event(session, _event())
        second = connectors.ingest_event(session, _event())
        assert first["duplicate"] is False
        assert len(first["triggers_fired"]) == 1
        assert first["triggers_fired"][0]["ok"] is True
        assert second["duplicate"] is True
        assert second["triggers_fired"] == []
        assert session.query(db.NotificationOutboxRecord).count() == 1
        rule = session.query(db.TriggerRuleRecord).one()
        assert rule.fired_count == 1
    finally:
        session.close()


def test_evaluate_triggers_skips_other_tenant_rules(fresh_db):
    session = db.SessionLocal()
    try:
        session.add(db.TriggerRuleRecord(
            name="tenant-a-only", source="", event_type="issue_updated",
            predicate="True", action_type="notify", notify_url="https://hooks.example/x",
            enabled=True, tenant_key="tenant-a",
        ))
        session.commit()
        fired = connectors.evaluate_triggers(session, _event(tenant_key="tenant-b"), db.EventRecord(
            source="jira", event_type="issue_updated", external_id="ISSUE-1",
            fingerprint="v2:x", payload_json="{}",
        ))
        assert fired == []
    finally:
        session.close()


def test_ingest_event_rejects_oversized_payload(fresh_db):
    session = db.SessionLocal()
    try:
        with pytest.raises(HTTPException) as excinfo:
            connectors.ingest_event(session, _event(payload={"blob": "x" * 65536}))
        assert excinfo.value.status_code == 413
    finally:
        session.close()


def test_connector_due_applies_exponential_backoff(fresh_db):
    record = db.ConnectorRecord(
        name="backoff", connector_type="generic_json", kind="polling",
        config_json='{"poll_interval": 30}',
        cursor_json='{"failures": 2, "last_batch": 0}',
        last_polled_at=datetime.now(timezone.utc) - timedelta(seconds=90),
    )
    now = datetime.now(timezone.utc)
    assert connectors.connector_due(record, now) is False  # 30 * 2**2 = 120s
    record.cursor_json = '{"failures": 0, "last_batch": 0}'
    assert connectors.connector_due(record, now) is True


def test_deliver_outbox_notification_revalidates_trigger_destinations(fresh_db, monkeypatch):
    session = db.SessionLocal()
    try:
        # No tenant credential/allowlist configured -> revalidation fails closed.
        result = connectors.deliver_outbox_notification(
            "https://hooks.example/x", {"event": "trigger.fired", "tenant": "acme"},
            session,
        )
        assert result.success is False
        assert result.attempts == 1
        assert "credential reference" in (result.last_error or "")
        # Plain (non-trigger) payloads skip revalidation and go straight out.
        delivered = {}
        from src import notify

        def fake_deliver(url, payload, *, max_attempts=1, **kwargs):
            delivered["url"] = url
            return notify.DeliveryResult(success=True, attempts=1, last_error=None)

        monkeypatch.setattr(notify, "deliver_webhook", fake_deliver)
        ok = connectors.deliver_outbox_notification(
            "https://hooks.example/x", {"event": "other"}, session,
        )
        assert ok.success is True
        assert delivered["url"] == "https://hooks.example/x"
    finally:
        session.close()


class _PollResponse:
    def __init__(self, chunks, status=200):
        self.chunks = chunks
        self.status_code = status
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def raise_for_status(self):
        pass

    def iter_content(self, **_):
        yield from self.chunks


@pytest.mark.parametrize("response, message", [
    (_PollResponse([b'{"issues":[]}'], 302), "not successful"),
    (_PollResponse([b"x" * (connectors.MAX_POLL_RESPONSE_BYTES + 1)]), "size limit"),
    (_PollResponse([b'{"issues":{}}']), "page shape"),
    (_PollResponse([b'{"issues":[null]}']), "invalid or excessive"),
])
def test_poll_response_is_bounded_and_closed(response, message):
    with pytest.raises(ValueError, match=message):
        connectors._read_poll_records(response, "issues")
    assert response.closed


def test_poll_response_success_is_closed():
    response = _PollResponse([b'{"issues":[{"id":"42"}]}'])
    assert connectors._read_poll_records(response, "issues") == [{"id": "42"}]
    assert response.closed


def test_jira_continuation_token_and_existing_identity(monkeypatch):
    import json

    calls = []
    pages = [
        {"issues": [{"id": "42", "fields": {}}], "isLast": False, "nextPageToken": "page-two"},
        {"issues": [{"id": "43", "fields": {}}], "isLast": True},
    ]

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return _PollResponse([json.dumps(pages.pop(0)).encode()])

    monkeypatch.setattr(connectors.requests, "get", get)
    connector = connectors.JiraConnector("https://jira.example", "test", "test", "project = OPS")
    first, cursor = connector.fetch_page(None, None)
    second, end = connector.fetch_page(None, cursor)
    assert [first[0].external_id, second[0].external_id] == ["42", "43"]
    assert cursor == "page-two" and end is None
    assert calls[0][0].endswith("/rest/api/3/search/jql")
    assert calls[1][1]["params"]["nextPageToken"] == cursor
    assert calls[1][1]["allow_redirects"] is False


def test_servicenow_keyset_page(monkeypatch):
    import json

    calls = []
    rows = [{"sys_id": f"{i:032x}", "number": f"INC{i}"} for i in range(1, 51)]

    def get(url, **kwargs):
        calls.append(kwargs["params"])
        return _PollResponse([json.dumps({"result": rows if len(calls) == 1 else []}).encode()])

    monkeypatch.setattr(connectors.requests, "get", get)
    connector = connectors.ServiceNowConnector("https://snow.example", "test", "test")
    first, cursor = connector.fetch_page(None, None)
    assert len(first) == 50 and first[0].external_id == "INC1"
    assert cursor == f"{50:032x}"
    assert connector.fetch_page(None, cursor) == ([], None)
    assert calls[1]["sysparm_query"] == f"sys_id>{cursor}^ORDERBYsys_id"
