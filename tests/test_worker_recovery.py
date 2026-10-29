"""Crash-recovery regressions for the lease-based workflow job queue."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from src import api, db, jobs, worker
from src.actions import ActionResult

ONBOARDING = {
    "request_type": "onboarding",
    "employee_name": "Jane Doe",
    "employee_email": "jane@example.com",
    "department": "Sales",
    "requester_id": 3,
    "start_date": "2026-11-02",
}


class FakeAdapter:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def execute(self, action, context):
        self.calls.append((action, context))
        if self.error:
            raise RuntimeError(self.error)
        return ActionResult(action=action, detail="fake action completed")


@pytest.fixture
def client(tmp_path):
    db.use_database(f"sqlite:///{tmp_path}/recovery.db")
    db.Base.metadata.create_all(bind=db.engine)
    return TestClient(api.app)


def _create_onboarding_item(client):
    created = client.post("/onboarding-requests", json=ONBOARDING)
    assert created.status_code == 201
    body = created.json()
    item = next(item for item in body["checklist"] if not item["requires_approval"])
    return body["id"], item["id"]


def _enqueue(client, request_id, item_id):
    response = client.post(f"/onboarding-requests/{request_id}/items/{item_id}/jobs")
    assert response.status_code == 202
    return response.json()["id"]


def test_expired_running_lease_is_recovered_by_the_next_worker(client, monkeypatch):
    """A worker that crashes while running is replaced; the job completes once."""
    request_id, item_id = _create_onboarding_item(client)
    job_id = _enqueue(client, request_id, item_id)
    monkeypatch.setattr(worker, "ACTION_ADAPTER", FakeAdapter())

    # First worker claims the job and then dies before executing anything.
    session = db.SessionLocal()
    try:
        claimed = jobs.claim_job(session, worker_id="crashed-worker")
        assert claimed is not None and claimed.id == job_id
        session.commit()
    finally:
        session.close()

    # Crash aftermath: the lease stays expired on disk (heartbeat stopped).
    session = db.SessionLocal()
    try:
        job = session.get(db.WorkflowJobRecord, job_id)
        job.lease_expires_at = db.utcnow() - timedelta(seconds=1)
        session.commit()
    finally:
        session.close()

    processed = worker.run_once(worker_id="recovery-worker")
    assert processed is not None
    assert processed.id == job_id
    assert processed.status == "succeeded"
    assert processed.attempts == 2

    stored = client.get(f"/workflow-jobs/{job_id}").json()
    assert stored["status"] == "succeeded"
    detail = client.get(f"/onboarding-requests/{request_id}").json()
    done = next(item for item in detail["checklist"] if item["id"] == item_id)
    assert done["status"] == "completed"


def test_stale_worker_cannot_complete_after_recovery(client, monkeypatch):
    """The late finisher of a crashed attempt is fenced by lease token."""
    request_id, item_id = _create_onboarding_item(client)
    job_id = _enqueue(client, request_id, item_id)
    adapter = FakeAdapter()
    monkeypatch.setattr(worker, "ACTION_ADAPTER", adapter)

    session = db.SessionLocal()
    try:
        claimed = jobs.claim_job(session, worker_id="crashed-worker")
        assert claimed is not None
        stale_token = claimed.lease_owner_token
        job = session.get(db.WorkflowJobRecord, job_id)
        job.lease_expires_at = db.utcnow() - timedelta(seconds=1)
        session.commit()
    finally:
        session.close()

    # Recovery worker re-claims and finishes the job.
    processed = worker.run_once(worker_id="recovery-worker")
    assert processed is not None and processed.status == "succeeded"

    # The crashed worker wakes up and still tries to complete its attempt.
    session = db.SessionLocal()
    try:
        completed = jobs.complete_job(session, job_id, stale_token, result={"late": True})
        session.commit()
    finally:
        session.close()
    assert completed is False
    final = client.get(f"/workflow-jobs/{job_id}").json()
    assert final["status"] == "succeeded"
    assert final["attempts"] == 2  # one claim per crashed and recovered attempt
    detail = client.get(f"/onboarding-requests/{request_id}").json()
    done = next(item for item in detail["checklist"] if item["id"] == item_id)
    assert done["status"] == "completed"


def test_failed_recovery_attempt_falls_back_to_retry_backoff(client, monkeypatch):
    """When the recovered attempt also fails, the retry budget still applies."""
    request_id, item_id = _create_onboarding_item(client)
    job_id = _enqueue(client, request_id, item_id)
    monkeypatch.setattr(worker, "ACTION_ADAPTER", FakeAdapter(error="automation down"))

    session = db.SessionLocal()
    try:
        assert jobs.claim_job(session, worker_id="crashed-worker") is not None
        session.commit()
        job = session.get(db.WorkflowJobRecord, job_id)
        job.lease_expires_at = db.utcnow() - timedelta(seconds=1)
        session.commit()
    finally:
        session.close()

    processed = worker.run_once(worker_id="recovery-worker")
    assert processed is not None and processed.status == "retry_scheduled"
    stored = client.get(f"/workflow-jobs/{job_id}").json()
    assert stored["status"] == "retry_scheduled"
    assert stored["attempts"] == 2
