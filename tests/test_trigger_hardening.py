"""Hardening tests for v1.4 triggers: mapping validation audit, webhook input
edge cases, and proof that webhook secrets/signatures never leak.

Runs against SQLite always and against PostgreSQL too when
``FLOWOPS_TEST_POSTGRES_URL`` is set.
"""

import hashlib
import hmac
import json
import logging
import os
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src import api, db, jobs, triggers, worker

SECRET = "whsec-LEAKCANARY-7f3a9c1e5b"
PG_URL = os.environ.get("FLOWOPS_TEST_POSTGRES_URL")

BACKENDS = ["sqlite"] + (["postgres"] if PG_URL else [])


@pytest.fixture(params=BACKENDS)
def client(request, tmp_path):
    if request.param == "postgres":
        db.use_database(PG_URL)
        db.Base.metadata.drop_all(bind=db.engine)
        db.Base.metadata.create_all(bind=db.engine)
    else:
        db.use_database(f"sqlite:///{tmp_path}/hardening.db")
        db.Base.metadata.create_all(bind=db.engine)
        db._ensure_tables()
    return TestClient(api.app, raise_server_exceptions=False)


def _sign(body: bytes, secret: str = SECRET, ts: int | None = None) -> dict[str, str]:
    ts = int(time.time()) if ts is None else ts
    digest = hmac.new(secret.encode(), str(ts).encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"X-FlowOps-Timestamp": str(ts), "X-FlowOps-Signature": f"sha256={digest}"}


def _setup(client, mapping=None):
    r = client.post("/event-sources", json={"key": "src", "name": "Source", "secret": SECRET})
    assert r.status_code == 201, r.text
    assert SECRET not in r.text
    r = client.post(
        "/triggers",
        json={
            "key": "trg",
            "name": "Trigger",
            "source_key": "src",
            "event_type": "user.created",
            "target_type": "ticket",
            "mapping_json": mapping or {"subject": "$event.data.subject", "body": "Auto: $trigger.name"},
        },
    )
    assert r.status_code == 201, r.text


# --------------------------------------------------------------------------
# Mapping validation audit
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "mapping",
    [
        {"a": "__import__('os').system('id')"},
        {"a": "eval(1)"},
        {"a": "EXEC something"},
        {"a": "$event.__class__"},
        {"a": "$event.data.__dict__"},
        {"a": "$event._private"},
        {"a": "$event."},
        {"a": "$event"},
        {"a": "$event..x"},
        {"a": "$event.a[0]"},
        {"a": "$event.a;b"},
        {"a": "$trigger.secret"},
        {"a": "$trigger.mapping_json"},
        {"a": "$source.secret"},
        {"a": "${event.x}"},
        {"a": "$env.HOME"},
        {"a": [["$event.__class__"]]},
        {"a": [{"b": "$trigger.secret"}]},
        {"$event.x": "literal"},
        {"__class__": "x"},
        {"": "x"},
        {1: "x"},
        {"a": float("nan")},
        {"a": float("inf")},
        {"a": {1, 2}},
        {"a": b"bytes"},
        {"a": object()},
    ],
)
def test_validate_mapping_rejects(mapping):
    with pytest.raises(triggers.MappingError):
        triggers.validate_mapping(mapping)


@pytest.mark.parametrize(
    "mapping",
    [
        {"a": "important update"},  # previously rejected: substring 'import'
        {"a": "executive onboarding"},  # previously rejected: substring 'exec'
        {"a": "evaluation period"},
        {"a": "$event.data.user_name", "b": "$trigger.key", "c": "$trigger.event_type"},
        {"a": [1, 2.5, True, None, "x", ["$event.a"], {"b": "$trigger.name"}]},
        {"a": {"b": {"c": "$event.x.y.z"}}},
        {"a": "cost is $5"},  # not a reference: does not start with '$'
    ],
)
def test_validate_mapping_accepts(mapping):
    triggers.validate_mapping(mapping)


def test_validate_mapping_bounds_depth_and_size():
    deep: dict = {}
    node = deep
    for _ in range(triggers.MAX_MAPPING_DEPTH + 2):
        node["n"] = {}
        node = node["n"]
    with pytest.raises(triggers.MappingError, match="depth"):
        triggers.validate_mapping(deep)
    with pytest.raises(triggers.MappingError, match="nodes"):
        triggers.validate_mapping({"a": list(range(triggers.MAX_MAPPING_NODES + 1))})
    with pytest.raises(triggers.MappingError):
        triggers.validate_mapping(["not", "a", "dict"])


def test_bad_mapping_rejected_on_create_and_update(client):
    _setup(client)
    bad = {"subject": "$trigger.secret"}
    r = client.post(
        "/triggers",
        json={"key": "t-bad", "name": "x", "source_key": "src", "event_type": "e",
              "target_type": "ticket", "mapping_json": bad},
    )
    assert r.status_code == 422
    assert client.get("/triggers").json()[0]["key"] == "trg"
    assert len(client.get("/triggers").json()) == 1
    r = client.put("/triggers/trg", json={"mapping_json": {"a": "$event.__class__"}})
    assert r.status_code == 422
    assert client.get("/triggers").json()[0]["mapping_json"]["subject"] == "$event.data.subject"


def test_mapping_cannot_reach_source_secret(client):
    _setup(client, mapping={"subject": "$event.data.subject", "who": "$trigger.source_key"})
    r = client.post("/triggers/trg/test", json={"data": {"subject": "hi"}})
    assert r.status_code == 200
    assert SECRET not in r.text


# --------------------------------------------------------------------------
# Webhook input edge cases (previously 500s, esp. on PostgreSQL)
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "body",
    [b"[1, 2]", b'"str"', b"null", b"\xff\xfe\x00", b'{"event_id": 123}',
     b'{"event_id": "' + b"a" * 129 + b'"}', b'{"event_id": ["x"]}'],
)
def test_webhook_rejects_bad_input_with_422(client, body):
    _setup(client)
    r = client.post("/webhooks/triggers/trg", content=body, headers=_sign(body))
    assert r.status_code == 422, r.text


def test_signature_verification_edge_cases():
    body = b"\xff\xfenot-utf8"
    ts = int(time.time())
    sig = _sign(body, ts=ts)["X-FlowOps-Signature"]
    assert triggers.verify_signature(body, sig, SECRET, ts)
    assert not triggers.verify_signature(body, sig, "other", ts)
    assert not triggers.verify_signature(body, "sha256=é" * 10, SECRET, ts)
    assert not triggers.verify_signature(body, sig.upper(), SECRET, ts)
    assert not triggers.verify_signature(body, sig[7:], SECRET, ts)
    assert not triggers.verify_signature(body, sig, SECRET, ts - 301)


# --------------------------------------------------------------------------
# Secret / signature redaction
# --------------------------------------------------------------------------

def test_webhook_secret_and_signature_never_leak(client, caplog):
    caplog.set_level(logging.DEBUG)
    _setup(client)
    body = json.dumps({"event_id": "evt-1", "data": {"subject": "VPN broken"}}).encode()
    headers = _sign(body)
    signature = headers["X-FlowOps-Signature"]
    sig_hex = signature[len("sha256="):]
    needles = (SECRET, sig_hex)

    responses = []
    r = client.post("/webhooks/triggers/trg", content=body, headers=headers)
    assert r.status_code == 200, r.text
    responses.append(r)
    responses.append(client.post("/webhooks/triggers/trg", content=body, headers=headers))  # replay
    bad = client.post("/webhooks/triggers/trg", content=body,
                      headers={**headers, "X-FlowOps-Signature": "sha256=" + "0" * 64})
    assert bad.status_code == 401
    responses.append(bad)
    worker.run_once(worker_id="hardening")

    for path in ("/event-sources", "/triggers", "/triggers/trg/deliveries", "/audit",
                 "/ui/triggers", "/ui/triggers/trg", "/metrics"):
        resp = client.get(path)
        assert resp.status_code in (200, 404), (path, resp.status_code)
        responses.append(resp)
    responses.append(client.post("/triggers/trg/test", json={"data": {"subject": "x"}}))
    responses.append(client.post("/event-sources", json={"key": "src", "name": "n", "secret": SECRET}))  # 409

    for resp in responses:
        for needle in needles:
            assert needle not in resp.text, resp.request.url
            assert all(needle not in v for v in resp.headers.values())

    for needle in needles:
        assert needle not in caplog.text

    session = db.SessionLocal()
    try:
        deliveries = session.execute(select(db.TriggerDeliveryRecord)).scalars().all()
        assert deliveries and deliveries[0].status in ("succeeded", "queued", "failed")
        logs = session.execute(select(db.ExecutionLogRecord)).scalars().all()
        assert any(log.action == "trigger.delivery.received" for log in logs)
        job_rows = session.execute(select(db.WorkflowJobRecord)).scalars().all()
        stored = [d.payload_json + (d.error or "") for d in deliveries]
        stored += [json.dumps({c.name: str(getattr(l, c.name)) for c in l.__table__.columns}) for l in logs]
        stored += [json.dumps({c.name: str(getattr(j, c.name)) for c in j.__table__.columns}) for j in job_rows]
        for blob in stored:
            for needle in needles:
                assert needle not in blob
        source = session.get(db.EventSourceRecord, "src")
        assert SECRET not in repr(source)
    finally:
        session.close()


@pytest.mark.parametrize("missing", ["name", "key"])
def test_validation_errors_do_not_echo_secret(client, missing):
    payload = {"key": "s", "name": "n", "secret": SECRET}
    payload.pop(missing)
    r = client.post("/event-sources", json=payload)
    assert r.status_code == 422
    assert SECRET not in r.text
    assert "[REDACTED]" in r.text


def test_validation_error_on_secret_field_itself_is_redacted(client):
    r = client.post("/event-sources", json={"key": "s", "name": "n", "secret": [SECRET]})
    assert r.status_code == 422
    assert SECRET not in r.text
