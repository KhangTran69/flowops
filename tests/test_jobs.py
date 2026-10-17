"""Tests for the durable workflow job queue and worker."""

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
    db.use_database(f"sqlite:///{tmp_path}/jobs.db")
    db.Base.metadata.create_all(bind=db.engine)
    return TestClient(api.app)


def _create_onboarding_item(client):
    created = client.post("/onboarding-requests", json=ONBOARDING)
    assert created.status_code == 201
    body = created.json()
    item = next(item for item in body["checklist"] if not item["requires_approval"])
    return body["id"], item["id"]


def test_enqueue_and_worker_completes_onboarding_item(client, monkeypatch):
    request_id, item_id = _create_onboarding_item(client)
    adapter = FakeAdapter()
    monkeypatch.setattr(worker, "ACTION_ADAPTER", adapter)

    response = client.post(
        f"/onboarding-requests/{request_id}/items/{item_id}/jobs"
    )
    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "queued"
    assert job["job_type"] == jobs.JOB_ONBOARDING_ITEM

    processed = worker.run_once(worker_id="test-worker")
    assert processed is not None
    assert processed.id == job["id"]
    assert processed.status == "succeeded"
    assert adapter.calls

    detail = client.get(f"/onboarding-requests/{request_id}").json()
    done = next(item for item in detail["checklist"] if item["id"] == item_id)
    assert done["status"] == "completed"
    stored = client.get(f"/workflow-jobs/{job['id']}").json()
    assert stored["status"] == "succeeded"
    assert stored["attempts"] == 1


def test_enqueue_is_idempotent_per_item(client):
    request_id, item_id = _create_onboarding_item(client)
    first = client.post(f"/onboarding-requests/{request_id}/items/{item_id}/jobs")
    second = client.post(f"/onboarding-requests/{request_id}/items/{item_id}/jobs")
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert len(client.get("/workflow-jobs").json()) == 1


def test_stale_worker_cannot_complete_after_takeover(client):
    request_id, item_id = _create_onboarding_item(client)
    job = client.post(
        f"/onboarding-requests/{request_id}/items/{item_id}/jobs"
    ).json()

    session = db.SessionLocal()
    try:
        claimed = jobs.claim_job(session, "worker-a", lease_seconds=1)
        assert claimed is not None and claimed.id == job["id"]
        token_a = claimed.lease_owner_token
        future = db.utcnow() + timedelta(minutes=5)
        taken = jobs.claim_job(session, "worker-b", now=future)
        assert taken is not None and taken.id == job["id"]
        token_b = taken.lease_owner_token
        assert token_b != token_a

        assert jobs.complete_job(session, job["id"], token_a) is False
        assert jobs.fail_job(session, job["id"], token_a, "late failure") is None
        assert jobs.complete_job(session, job["id"], token_b) is True
        session.commit()
    finally:
        session.close()


def test_worker_retries_then_dead_letters(client, monkeypatch):
    request_id, item_id = _create_onboarding_item(client)
    adapter = FakeAdapter(error="adapter exploded")
    monkeypatch.setattr(worker, "ACTION_ADAPTER", adapter)
    session = db.SessionLocal()
    try:
        job = jobs.enqueue_job(
            session,
            job_type=jobs.JOB_ONBOARDING_ITEM,
            payload={"request_id": request_id, "item_id": item_id, "action": "provision_hardware"},
            max_attempts=2,
        )
        session.commit()
        job_id = job.id
    finally:
        session.close()

    first = worker.run_once(worker_id="retry-worker")
    assert first is not None and first.status == "retry_scheduled"
    assert first.attempts == 1

    session = db.SessionLocal()
    try:
        row = session.get(db.WorkflowJobRecord, job_id)
        row.available_at = db.utcnow() - timedelta(seconds=1)
        session.commit()
    finally:
        session.close()

    second = worker.run_once(worker_id="retry-worker")
    assert second is not None and second.status == "failed"
    assert second.attempts == 2
    assert second.last_error == "adapter exploded"


def test_job_api_list_get_cancel_and_retry(client):
    request_id, item_id = _create_onboarding_item(client)
    created = client.post(
        f"/onboarding-requests/{request_id}/items/{item_id}/jobs"
    ).json()
    job_id = created["id"]

    assert client.get("/workflow-jobs").status_code == 200
    assert client.get(f"/workflow-jobs/{job_id}").json()["status"] == "queued"
    assert client.get("/workflow-jobs/999").status_code == 404

    cancelled = client.post(f"/workflow-jobs/{job_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert client.post(f"/workflow-jobs/{job_id}/cancel").status_code == 409
    assert client.post(f"/workflow-jobs/{job_id}/retry").status_code == 409

    session = db.SessionLocal()
    try:
        row = session.get(db.WorkflowJobRecord, job_id)
        row.status = "failed"
        row.last_error = "boom"
        session.commit()
    finally:
        session.close()

    retried = client.post(f"/workflow-jobs/{job_id}/retry")
    assert retried.status_code == 200
    assert retried.json()["status"] == "queued"
    assert retried.json()["attempts"] == 0


def test_jobs_page_lists_queue(client):
    request_id, item_id = _create_onboarding_item(client)
    client.post(f"/onboarding-requests/{request_id}/items/{item_id}/jobs")
    page = client.get("/ui/workflow-jobs")
    assert page.status_code == 200
    assert "Workflow jobs" in page.text
    assert "request #" in page.text


class SlowAdapter:
    def __init__(self):
        self.contexts = []
        self.delay = 0.2

    def execute(self, action, context):
        import time
        self.contexts.append(context)
        time.sleep(self.delay)
        return ActionResult(action=action, detail="slow fake")


def test_worker_heartbeats_and_passes_idempotency_key(client, monkeypatch):
    request_id, item_id = _create_onboarding_item(client)
    adapter = SlowAdapter()
    monkeypatch.setattr(worker, "ACTION_ADAPTER", adapter)
    calls = []
    original_heartbeat = jobs.heartbeat_job

    def counting_heartbeat(*args, **kwargs):
        calls.append(1)
        return original_heartbeat(*args, **kwargs)

    monkeypatch.setattr(jobs, "heartbeat_job", counting_heartbeat)
    job = client.post(
        f"/onboarding-requests/{request_id}/items/{item_id}/jobs"
    ).json()
    processed = worker.run_once(
        worker_id="heartbeat-worker",
        lease_seconds=1,
        heartbeat_interval=0.05,
    )
    assert processed is not None and processed.status == "succeeded"
    assert calls, "heartbeat should fire during a slow action"
    assert adapter.contexts
    assert adapter.contexts[0]["idempotency_key"] == f"onboarding:{request_id}:{item_id}"
    assert adapter.contexts[0]["job_id"] == job["id"]


def test_heartbeat_rejects_a_lost_lease(client):
    request_id, item_id = _create_onboarding_item(client)
    client.post(f"/onboarding-requests/{request_id}/items/{item_id}/jobs")
    session = db.SessionLocal()
    try:
        claimed = jobs.claim_job(session, "owner-a", lease_seconds=1)
        assert claimed is not None
        assert jobs.heartbeat_job(
            session, claimed.id, "not-the-owner", lease_seconds=1
        ) is False
        assert jobs.heartbeat_job(
            session, claimed.id, claimed.lease_owner_token or "", lease_seconds=1
        ) is True
        session.commit()
    finally:
        session.close()


def test_worker_terminal_failure_triggers_notification_webhook(client, monkeypatch):
    from src.notify import DeliveryResult

    request_id, item_id = _create_onboarding_item(client)
    job = client.post(
        f"/onboarding-requests/{request_id}/items/{item_id}/jobs"
    ).json()

    class AlwaysFailingAdapter:
        def execute(self, action, context):
            raise RuntimeError("adapter critical failure")

    webhook_calls = []

    def fake_deliver(url, payload):
        webhook_calls.append((url, payload))
        return DeliveryResult(success=True, attempts=1, last_error=None)

    monkeypatch.setattr(worker, "ACTION_ADAPTER", AlwaysFailingAdapter())
    monkeypatch.setenv("FLOWOPS_ACTION_FAILURE_WEBHOOK_URL", "https://alert.example/worker-fail")
    monkeypatch.setattr("src.notify.deliver_webhook", fake_deliver)

    session = db.SessionLocal()
    try:
        row = session.get(db.WorkflowJobRecord, job["id"])
        row.max_attempts = 2
        session.commit()
    finally:
        session.close()

    # Exhaust retries (max_attempts=2)
    first = worker.run_once(worker_id="fail-worker-1")
    assert first is not None and first.status == "retry_scheduled"
    assert len(webhook_calls) == 0  # not terminal yet

    # Advance availability
    session = db.SessionLocal()
    try:
        row = session.get(db.WorkflowJobRecord, job["id"])
        row.available_at = db.utcnow() - timedelta(seconds=1)
        session.commit()
    finally:
        session.close()

    second = worker.run_once(worker_id="fail-worker-2")
    assert second is not None and second.status == "failed"
    assert len(webhook_calls) == 1
    url, payload = webhook_calls[0]
    assert url == "https://alert.example/worker-fail"
    assert payload["event"] == "workflow.job.failed"
    assert payload["job_id"] == job["id"]
    assert "critical failure" in payload["error"]

    logs = client.get("/execution-logs").json()
    assert any(log["action"] == "notification.action_failure" for log in logs)


def test_control_plane_propagates_correlation_and_exposes_history(client):
    request_id, item_id = _create_onboarding_item(client)
    response = client.post(
        f"/onboarding-requests/{request_id}/items/{item_id}/jobs",
        headers={"X-Correlation-ID": "control-plane-test"},
    )
    assert response.status_code == 202
    job_id = response.json()["id"]
    history = client.get(f"/workflow-jobs/{job_id}/history")
    assert history.status_code == 200
    assert history.json() == []
    metrics = client.get("/metrics").text
    assert "flowops_workflow_job_attempts_total" in metrics
