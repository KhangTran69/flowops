"""PostgreSQL coverage for durable workflow job leases and fencing."""

import os
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from src import api, db, jobs

POSTGRES_URL = os.environ.get("FLOWOPS_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL, reason="FLOWOPS_TEST_POSTGRES_URL is not set"
)

ONBOARDING = {
    "request_type": "onboarding",
    "employee_name": "Jane Doe",
    "employee_email": "jane@example.com",
    "department": "Sales",
    "requester_id": 3,
    "start_date": "2026-11-02",
}


@pytest.fixture
def client():
    db.use_database(POSTGRES_URL)
    db.Base.metadata.drop_all(bind=db.engine)
    db.Base.metadata.create_all(bind=db.engine)
    try:
        yield TestClient(api.app)
    finally:
        db.Base.metadata.drop_all(bind=db.engine)


def test_postgres_claim_takeover_and_completion_fence(client):
    session = db.SessionLocal()
    try:
        job = jobs.enqueue_job(
            session,
            job_type=jobs.JOB_ONBOARDING_ITEM,
            payload={"request_id": 1, "item_id": 1, "action": "provision_hardware"},
            max_attempts=3,
        )
        session.commit()
        job_id = job.id

        first = jobs.claim_job(session, "worker-a", lease_seconds=1)
        assert first is not None and first.id == job_id
        stale_token = first.lease_owner_token
        future = db.utcnow() + timedelta(minutes=5)

        second = jobs.claim_job(session, "worker-b", now=future)
        assert second is not None and second.id == job_id
        current_token = second.lease_owner_token
        assert current_token != stale_token

        assert jobs.complete_job(session, job_id, stale_token) is False
        assert jobs.fail_job(session, job_id, stale_token, "late") is None
        assert jobs.complete_job(session, job_id, current_token) is True
        session.commit()
        session.expire_all()
        row = session.get(db.WorkflowJobRecord, job_id)
        assert row.status == "succeeded"
        assert row.attempts == 2
    finally:
        session.close()


def test_postgres_enqueue_and_cancel_via_api(client):
    created = client.post("/onboarding-requests", json=ONBOARDING)
    assert created.status_code == 201
    request_id = created.json()["id"]
    item_id = next(
        item["id"] for item in created.json()["checklist"] if not item["requires_approval"]
    )
    queued = client.post(
        f"/onboarding-requests/{request_id}/items/{item_id}/jobs"
    )
    assert queued.status_code == 202
    job_id = queued.json()["id"]
    assert client.get(f"/workflow-jobs/{job_id}").json()["status"] == "queued"
    cancelled = client.post(f"/workflow-jobs/{job_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_postgres_expired_lease_cannot_be_renewed_or_finalized(client):
    with db.SessionLocal() as session:
        row = jobs.enqueue_job(session, job_type=jobs.JOB_TRIGGER_EXECUTE, payload={})
        session.commit()
        at = db.utcnow()
        claimed = jobs.claim_job(session, "expired-owner", now=at, lease_seconds=1)
        assert claimed is not None and claimed.id == row.id
        token = claimed.lease_owner_token
        expired = at + timedelta(seconds=1)
        assert not jobs.heartbeat_job(session, row.id, token, now=expired)
        assert not jobs.complete_job(session, row.id, token, now=expired)
        assert jobs.fail_job(session, row.id, token, "late failure", now=expired) is None
        session.commit()
        replacement = jobs.claim_job(session, "replacement", now=expired)
        assert replacement is not None and replacement.lease_owner_token != token
        assert jobs.complete_job(session, row.id, replacement.lease_owner_token, now=expired)
        session.commit()


def test_postgres_exhausted_abandoned_claim_is_terminalized_without_another_attempt(client):
    with db.SessionLocal() as session:
        row = jobs.enqueue_job(session, job_type=jobs.JOB_TRIGGER_EXECUTE, payload={}, max_attempts=1)
        session.commit()
        job_id = row.id
        at = db.utcnow()
        claimed = jobs.claim_job(session, "abandoned", now=at, lease_seconds=1)
        assert claimed is not None
        expired = at + timedelta(seconds=2)
        assert jobs.claim_job(session, "too-late", now=expired) is None
        terminal = jobs.reap_exhausted_job(session, now=expired)
        assert terminal is not None and terminal.id == job_id
        assert terminal.attempts == 1 and terminal.status == "failed"
        assert terminal.lease_owner_token is None
        session.commit()
        assert jobs.reap_exhausted_job(session, now=expired) is None
