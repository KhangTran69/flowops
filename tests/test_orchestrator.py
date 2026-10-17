"""Offline tests for the v1.2 DB-backed DAG orchestrator."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src import api, db, jobs, scheduler, worker


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWOPS_AUTH_REQUIRED", "false")
    monkeypatch.setenv("FLOWOPS_UI_AUTH_REQUIRED", "false")
    monkeypatch.setenv("FLOWOPS_RATE_LIMIT_PER_MINUTE", "0")
    monkeypatch.setenv("FLOWOPS_DAG_MAX_PARALLEL", "4")
    db.use_database(f"sqlite:///{tmp_path}/orchestrator.db")
    with TestClient(api.app) as test_client:
        yield test_client


def _create(client, template="onboarding-dag"):
    response = client.post(
        "/onboarding-requests",
        json={
            "request_type": "onboarding",
            "employee_name": "Dana Engineer",
            "employee_email": "dana@example.com",
            "department": "Engineering",
            "requester_id": 7,
            "template": template,
        },
    )
    assert response.status_code == 201
    body = response.json()
    return body["id"], {item["step_key"]: item for item in body["checklist"]}


def _start(client, request_id):
    response = client.post(f"/onboarding-requests/{request_id}/run")
    assert response.status_code == 200
    return response.json()


def test_start_schedules_only_ready_non_gated_steps(client):
    request_id, items = _create(client)
    run = _start(client, request_id)
    assert run["status"] == "running"
    assert [step["checklist_item_id"] for step in run["steps"]] == [
        items["provision_laptop"]["id"]
    ]
    jobs = client.get("/workflow-jobs").json()
    assert len(jobs) == 1
    assert jobs[0]["status"] == "queued"


def test_approval_schedules_gated_step_exactly_once(client):
    request_id, items = _create(client)
    run = _start(client, request_id)
    create_id = items["create_user_account"]["id"]
    client.post(f"/onboarding-requests/{request_id}/items/{create_id}/approve")
    jobs_after_approval = client.get("/workflow-jobs").json()
    assert len(jobs_after_approval) == 2

    session = db.SessionLocal()
    try:
        scheduler.tick(session, run["id"])
        scheduler.tick(session, run["id"])
    finally:
        session.close()
    assert len(client.get("/workflow-jobs").json()) == 2


def test_worker_advances_run_and_unblocks_dependent_steps(client):
    request_id, items = _create(client)
    _start(client, request_id)
    assert worker.run_once().status == "succeeded"

    client.post(
        f"/onboarding-requests/{request_id}/items/"
        f"{items['create_user_account']['id']}/approve"
    )
    assert worker.run_once().status == "succeeded"
    run = client.get(f"/onboarding-requests/{request_id}/run").json()
    statuses = {row["checklist_item_id"]: row["status"] for row in run["steps"]}
    assert statuses[items["create_user_account"]["id"]] == "succeeded"
    # Mailbox has no approval gate and becomes ready; licences remain gated.
    assert items["configure_mailbox"]["id"] in statuses
    assert items["grant_licenses"]["id"] not in statuses


def test_pause_blocks_scheduling_and_resume_continues(client):
    request_id, items = _create(client)
    run = _start(client, request_id)
    paused = client.post(f"/workflow-runs/{run['id']}/pause").json()
    assert paused["status"] == "paused"
    client.post(
        f"/onboarding-requests/{request_id}/items/"
        f"{items['create_user_account']['id']}/approve"
    )
    assert len(client.get("/workflow-jobs").json()) == 1

    resumed = client.post(f"/workflow-runs/{run['id']}/resume").json()
    assert resumed["status"] == "running"
    assert len(client.get("/workflow-jobs").json()) == 2


def test_cancel_cancels_queued_jobs_and_prevents_scheduling(client):
    request_id, items = _create(client)
    run = _start(client, request_id)
    cancelled = client.post(f"/workflow-runs/{run['id']}/cancel").json()
    assert cancelled["status"] == "cancelled"
    jobs = client.get("/workflow-jobs").json()
    assert all(job["status"] == "cancelled" for job in jobs)

    client.post(
        f"/onboarding-requests/{request_id}/items/"
        f"{items['create_user_account']['id']}/approve"
    )
    assert len(client.get("/workflow-jobs").json()) == 1


def test_terminal_failure_cascades_and_fails_run(client, monkeypatch):
    request_id, items = _create(client)
    run = _start(client, request_id)

    class BrokenAdapter:
        def execute(self, action, context):
            raise RuntimeError("adapter exploded")

    monkeypatch.setattr(worker, "ACTION_ADAPTER", BrokenAdapter())
    session = db.SessionLocal()
    try:
        job = session.execute(
            select(db.WorkflowJobRecord).where(db.WorkflowJobRecord.status == "queued")
        ).scalar_one()
        item = session.get(db.ChecklistItemRecord, job.payload_json and items["provision_laptop"]["id"])
        item.status = "failed"
        session.commit()
        scheduler.advance_from_job(session, job, success=False, error="adapter exploded")
        session.commit()
    finally:
        session.close()
    updated = client.get(f"/onboarding-requests/{request_id}/run").json()
    assert updated["status"] == "failed"


def test_claim_is_exclusive_and_stale_lease_can_be_taken_over(client):
    request_id, _ = _create(client)
    run = _start(client, request_id)
    session = db.SessionLocal()
    try:
        first = scheduler.claim_run(
            session, run_id=run["id"], worker_id="scheduler-a"
        )
        second = scheduler.claim_run(
            session, run_id=run["id"], worker_id="scheduler-b"
        )
        assert first is not None
        assert second is None
        session.rollback()
        record = session.get(db.WorkflowRunRecord, run["id"])
        record.lease_owner_token = "stale-owner"
        record.lease_expires_at = db.utcnow() - timedelta(seconds=120)
        session.commit()
        takeover = scheduler.claim_run(
            session,
            run_id=run["id"],
            worker_id="scheduler-c",
            now=db.utcnow(),
        )
        assert takeover is not None
        assert takeover.lease_owner_token.startswith("scheduler-c:")
    finally:
        session.close()


def test_flat_template_remains_compatible(client):
    request_id, items = _create(client, template=None)
    run = _start(client, request_id)
    # Non-gated flat steps are scheduled; gated steps wait for approval.
    scheduled = {step["checklist_item_id"] for step in run["steps"]}
    assert items["provision_laptop"]["id"] in scheduled
    assert items["create_user_account"]["id"] not in scheduled


def test_workflow_runs_ui_and_metrics(client):
    request_id, _ = _create(client)
    _start(client, request_id)
    page = client.get("/ui/workflow-runs")
    assert page.status_code == 200
    assert "Workflow runs" in page.text
    detail = client.get(f"/ui/onboarding-requests/{request_id}")
    assert "Workflow run" in detail.text
    metrics = client.get("/metrics").text
    assert "flowops_dag_runs" in metrics
    assert "flowops_dag_steps" in metrics
    assert "flowops_dag_scheduler_ticks_total" in metrics
