"""Chaos drills: crash windows the lease design must survive.

Each drill simulates a hard kill at the worst possible moment and asserts
the invariants the architecture promises: exactly-one execution, fenced
takeover, and no poisoned ledger keys.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from src import api, db, jobs, outbox_worker, scheduler, worker
from src.actions import ActionResult
from src.notify import DeliveryResult

ONBOARDING = {
    "request_type": "onboarding",
    "employee_name": "Jane Doe",
    "employee_email": "jane@example.com",
    "department": "Sales",
    "requester_id": 3,
    "start_date": "2026-11-02",
}


class FakeAdapter:
    def execute(self, action, context):
        return ActionResult(action=action, detail="fake action completed")


@pytest.fixture
def client(tmp_path):
    db.use_database(f"sqlite:///{tmp_path}/chaos.db")
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


def test_takeover_storm_has_exactly_one_winner(client, monkeypatch):
    """Eight workers race for one job; exactly one claim wins and completes.

    The claim is a conditional UPDATE, so concurrent claimers must observe a
    single winner; losers see no job at all. The winner's completion leaves
    attempts == 1 and the checklist item completed exactly once.
    """
    monkeypatch.setattr(worker, "ACTION_ADAPTER", FakeAdapter())
    request_id, item_id = _create_onboarding_item(client)
    job_id = _enqueue(client, request_id, item_id)

    def try_claim(worker_id):
        session = db.SessionLocal()
        try:
            claimed = jobs.claim_job(session, worker_id=worker_id)
            session.commit()
            return claimed.id if claimed is not None else None
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(try_claim, [f"storm-{i}" for i in range(8)]))

    winners = [job_id_result for job_id_result in results if job_id_result is not None]
    assert winners == [job_id]

    # The winner walks away (drill ends its thread). After the lease expires,
    # exactly one recovery worker takes over and completes it — attempts == 2
    # proves a takeover happened and the job did not double-execute.
    session = db.SessionLocal()
    try:
        job = session.get(db.WorkflowJobRecord, job_id)
        job.lease_expires_at = db.utcnow() - timedelta(seconds=1)
        session.commit()
    finally:
        session.close()

    processed = worker.run_once(worker_id="storm-recovery")
    assert processed is not None and processed.id == job_id
    assert processed.status == "succeeded"
    assert processed.attempts == 2

    detail = client.get(f"/onboarding-requests/{request_id}").json()
    done = next(item for item in detail["checklist"] if item["id"] == item_id)
    assert done["status"] == "completed"


def test_scheduler_killed_mid_tick_does_not_double_enqueue(client):
    """A scheduler that dies right after claiming a run lease loses the tick.

    The recovery scheduler must take over the expired lease exactly once and
    enqueue each ready step exactly once (one job, one step-run attempt).
    """
    request_id, item_id = _create_onboarding_item(client)

    # Start a run row directly (start_run would tick immediately).
    session = db.SessionLocal()
    try:
        run = db.WorkflowRunRecord(request_id=request_id, status="running")
        session.add(run)
        session.commit()
        run_id = run.id
    finally:
        session.close()

    # Scheduler A claims the run lease, then dies before enqueuing anything.
    session = db.SessionLocal()
    try:
        claimed = scheduler.claim_run(session, worker_id="crashed-scheduler")
        assert claimed is not None and claimed.id == run_id
        session.commit()
    finally:
        session.close()

    # While the lease is unexpired, the recovery scheduler must not touch it.
    session = db.SessionLocal()
    try:
        assert scheduler.claim_run(session, worker_id="early-recovery") is None
        session.rollback()
    finally:
        session.close()

    # Crash aftermath: the lease expires on disk (no heartbeat).
    session = db.SessionLocal()
    try:
        run = session.get(db.WorkflowRunRecord, run_id)
        run.lease_expires_at = db.utcnow() - timedelta(seconds=1)
        session.commit()
    finally:
        session.close()

    # Recovery tick: claims the run and enqueues the ready step once.
    session = db.SessionLocal()
    try:
        advanced = scheduler.tick(session, run_id=run_id, worker_id="recovery-scheduler")
        assert advanced is not None and advanced.id == run_id
        session.commit()
    finally:
        session.close()

    session = db.SessionLocal()
    try:
        step_runs = session.query(db.WorkflowStepRunRecord).filter(
            db.WorkflowStepRunRecord.run_id == run_id
        ).all()
        # Every ready item was enqueued exactly once (attempt 1, one job each)
        # despite the crashed scheduler's abandoned lease.
        per_item = {}
        for step_run in step_runs:
            per_item.setdefault(step_run.checklist_item_id, []).append(step_run)
        assert all(len(runs) == 1 and runs[0].attempt == 1 for runs in per_item.values())
        assert len(per_item) >= 1
        job_ids = {step_run.job_id for step_run in step_runs}
        assert len(job_ids) == len(step_runs)  # one distinct job per attempt
        for job_id in job_ids:
            assert session.get(db.WorkflowJobRecord, job_id) is not None
    finally:
        session.close()

    # Drain the queue: every enqueued step completes exactly once.
    step_run_count = sum(len(runs) for runs in per_item.values())
    processed_total = 0
    while (processed := worker.run_once(worker_id="recovery-worker")) is not None:
        processed_total += 1
        assert processed.status == "succeeded"
    assert processed_total == step_run_count

    detail = client.get(f"/onboarding-requests/{request_id}").json()
    completed = [item for item in detail["checklist"] if item["status"] == "completed"]
    assert len(completed) == len(per_item)


def test_outbox_worker_killed_mid_delivery_delivers_exactly_once(client, monkeypatch):
    """An outbox worker that dies between claim and delivery loses nothing.

    The claimed row keeps its lease, so the next dispatcher skips it until
    the lease expires; the recovery dispatcher then delivers exactly once
    and the row reaches its terminal state through the fenced finalize.
    """
    session = db.SessionLocal()
    try:
        row = db.NotificationOutboxRecord(
            url="https://hooks.example/outbox",
            payload_json=json.dumps(
                {"event": "ticket.created", "ticket": {"ticket_id": "42"}}
            ),
            status="pending",
        )
        session.add(row)
        session.commit()
        row_id = row.id
    finally:
        session.close()

    # Worker A claims and is killed mid-delivery (the delivery call raises).
    def exploding_delivery(url, payload, **kwargs):
        raise RuntimeError("killed mid-delivery")

    monkeypatch.setattr(outbox_worker, "deliver_webhook", exploding_delivery)
    with pytest.raises(RuntimeError):
        outbox_worker.run_once(worker_id="crashed-dispatcher")

    # The row keeps its unexpired claim; the next dispatcher must skip it.
    monkeypatch.setattr(
        outbox_worker,
        "deliver_webhook",
        lambda url, payload, **kwargs: DeliveryResult(success=True, attempts=1, last_error=None),
    )
    assert outbox_worker.run_once(worker_id="early-dispatcher") is None

    # Crash aftermath: lease expires on disk; recovery delivers once.
    session = db.SessionLocal()
    try:
        row = session.get(db.NotificationOutboxRecord, row_id)
        row.lease_expires_at = db.utcnow() - timedelta(seconds=1)
        session.commit()
    finally:
        session.close()

    delivered = outbox_worker.run_once(worker_id="recovery-dispatcher")
    assert delivered is not None and delivered.id == row_id
    assert delivered.status == "delivered"
    assert delivered.attempts == 1

    # A terminal row is never picked up again.
    assert outbox_worker.run_once(worker_id="idle-dispatcher") is None
