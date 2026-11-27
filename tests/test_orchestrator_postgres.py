"""PostgreSQL coverage for orchestrator run leases and claims."""

import os
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from src import api, db, scheduler

POSTGRES_URL = os.environ.get("FLOWOPS_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL, reason="FLOWOPS_TEST_POSTGRES_URL is not set"
)


@pytest.fixture
def client():
    db.use_database(POSTGRES_URL)
    db.Base.metadata.drop_all(bind=db.engine)
    db.Base.metadata.create_all(bind=db.engine)
    try:
        yield TestClient(api.app)
    finally:
        db.Base.metadata.drop_all(bind=db.engine)


def test_postgres_run_claim_and_stale_takeover(client):
    created = client.post(
        "/onboarding-requests",
        json={
            "request_type": "onboarding",
            "employee_name": "Dana",
            "employee_email": "dana@example.com",
            "department": "Eng",
            "requester_id": 1,
            "template": "onboarding-dag",
        },
    )
    assert created.status_code == 201
    request_id = created.json()["id"]
    run = client.post(f"/onboarding-requests/{request_id}/run").json()
    assert run["status"] == "running"

    first_session = db.SessionLocal()
    second_session = db.SessionLocal()
    try:
        first = scheduler.claim_run(first_session, run_id=run["id"], worker_id="pg-a")
        first_session.commit()
        second = scheduler.claim_run(second_session, run_id=run["id"], worker_id="pg-b")
        assert first is not None
        assert second is None
        first_session.rollback()
        record = first_session.get(db.WorkflowRunRecord, run["id"])
        record.lease_owner_token = "stale"
        record.lease_expires_at = db.utcnow() - timedelta(seconds=120)
        first_session.commit()
        takeover = scheduler.claim_run(
            first_session, run_id=run["id"], worker_id="pg-c"
        )
        assert takeover is not None
        assert takeover.lease_owner_token.startswith("pg-c:")
    finally:
        first_session.close()
        second_session.close()


def test_concurrent_start_and_control_replay(client):
    from concurrent.futures import ThreadPoolExecutor
    created = client.post("/onboarding-requests", json={
        "request_type": "onboarding", "employee_name": "Concurrent",
        "employee_email": "concurrent@example.test", "department": "IT",
        "requester_id": 7, "template": "onboarding-dag",
    })
    assert created.status_code == 201, created.text
    start_path = f"/onboarding-requests/{created.json()['id']}/run"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.post(start_path), range(2)))
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert results[0].json()["id"] == results[1].json()["id"]
    run_id = results[0].json()["id"]
    path = f"/workflow-runs/{run_id}"
    headers = {"Idempotency-Key": "concurrent-pause"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        pauses = list(pool.map(lambda _: client.post(path + "/pause", headers=headers), range(2)))
    assert any(r.status_code == 200 for r in pauses)
    assert all(r.status_code in (200, 409) for r in pauses)
    resumed = client.post(path + "/resume", headers={"Idempotency-Key": "resume"})
    assert resumed.status_code == 200 and resumed.json()["status"] == "running"
    replay = client.post(path + "/pause", headers=headers)
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json()["status"] == "paused"
    assert client.get(path).json()["status"] == "running"
    assert client.post(path + "/cancel", headers=headers).status_code == 409
    assert client.post(path + "/cancel", headers={"Idempotency-Key": "cancel"}).json()["status"] == "cancelled"
    assert client.post(path + "/resume").status_code == 409
    session = db.SessionLocal()
    try:
        assert session.query(db.WorkflowRunRecord).count() == 1
        assert session.query(db.ExecutionLogRecord).filter_by(action="workflow.run.paused").count() == 1
    finally:
        session.close()
