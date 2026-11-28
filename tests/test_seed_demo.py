"""Offline tests for the portfolio demo seed script (scripts/seed_demo.py)."""

from datetime import timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from scripts.seed_demo import TENANT, TICKET_SPECS, seed
from src import api, db
from src.routing import route_ticket


def _aware(value):
    """SQLite returns naive datetimes; normalize for comparison with utcnow."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@pytest.fixture
def seeded_db(tmp_path):
    db.use_database(f"sqlite:///{tmp_path}/seed-demo.db")
    return tmp_path


def _count(session, model):
    return len(session.execute(select(model)).scalars().all())


def test_seed_twice_is_idempotent(seeded_db):
    first = seed()
    assert first["tickets"] == len(TICKET_SPECS)
    with db.SessionLocal() as session:
        baseline = _count(session, db.TicketRecord)
    second = seed()
    assert second == {"skipped": 1}
    with db.SessionLocal() as session:
        assert _count(session, db.TicketRecord) == baseline


def test_seed_reset_clears_stray_rows_and_reseeds(seeded_db):
    seed()
    with db.SessionLocal() as session:
        session.add(db.TicketRecord(
            ticket_id="STRAY-1", requester_id=1, title="stray", status="open",
            priority="normal", tenant_key=TENANT,
        ))
        session.commit()
    counts = seed(reset=True)
    assert counts["tickets"] == len(TICKET_SPECS)
    with db.SessionLocal() as session:
        assert session.scalar(
            select(db.TicketRecord).where(db.TicketRecord.ticket_id == "STRAY-1")
        ) is None
        assert _count(session, db.TicketRecord) == len(TICKET_SPECS)


def test_seed_routing_matches_live_rules(seeded_db):
    seed()
    with db.SessionLocal() as session:
        tickets = session.execute(
            select(db.TicketRecord).where(db.TicketRecord.tenant_key == TENANT)
        ).scalars().all()
    assert len(tickets) == len(TICKET_SPECS)
    for ticket in tickets:
        decision = route_ticket(ticket.title)
        assert (ticket.category, ticket.priority, ticket.queue) == (
            decision.category, decision.priority, decision.queue
        )


def test_seed_run_states_support_the_demo_journey(seeded_db):
    seed()
    with db.SessionLocal() as session:
        runs = session.execute(select(db.WorkflowRunRecord)).scalars().all()
        statuses = sorted(run.status for run in runs)
        assert statuses == ["paused", "running"]
        by_status = {run.status: run for run in runs}
        paused = by_status["paused"]
        paused_items = session.execute(
            select(db.ChecklistItemRecord).where(
                db.ChecklistItemRecord.request_id == paused.request_id
            )
        ).scalars().all()
        gated = [item for item in paused_items if item.requires_approval]
        assert {item.status for item in gated} == {"pending", "blocked"}
        awaiting = [item for item in gated if item.status == "pending"]
        assert [item.step_key for item in awaiting] == ["create_user_account"]
        assert awaiting[0].sla_minutes == 60
        timer = session.execute(
            select(db.WorkflowTimerRecord).where(
                db.WorkflowTimerRecord.request_id == paused.request_id,
                db.WorkflowTimerRecord.status == "pending",
            )
        ).scalars().one()
        assert _aware(timer.due_at) > db.utcnow()
        queued_jobs = session.execute(
            select(db.WorkflowJobRecord).where(
                db.WorkflowJobRecord.status == "queued"
            )
        ).scalars().all()
        assert len(queued_jobs) == 1
        assert queued_jobs[0].job_type == "onboarding.item.execute"
        failed_jobs = session.execute(
            select(db.WorkflowJobRecord).where(
                db.WorkflowJobRecord.status == "failed"
            )
        ).scalars().all()
        assert len(failed_jobs) == 1
        assert failed_jobs[0].attempts == failed_jobs[0].max_attempts
        dead_letters = session.execute(
            select(db.NotificationOutboxRecord).where(
                db.NotificationOutboxRecord.status == "failed"
            )
        ).scalars().all()
        assert len(dead_letters) == 1


def test_seed_populates_demo_surfaces(seeded_db):
    seed()
    client = TestClient(api.app)
    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "DEMO-0001" in dashboard.text
    jobs_page = client.get("/ui/workflow-jobs")
    assert jobs_page.status_code == 200
    assert "SimulatedActionError" in jobs_page.text
    assert "Priya Raman" in jobs_page.text
    notifications = client.get("/ui/notifications")
    assert notifications.status_code == 200
    assert "DEMO-0001" in notifications.text
    onboarding = client.get("/ui/onboarding-requests")
    assert onboarding.status_code == 200
    assert "Priya Raman" in onboarding.text
    assert "Marcus Webb" in onboarding.text
    runs_page = client.get("/ui/workflow-runs")
    assert runs_page.status_code == 200
    assert "paused" in runs_page.text
    definitions = client.get("/ui/workflow-definitions")
    assert definitions.status_code == 200
    assert "onboarding-dag" in definitions.text
    analytics = client.get("/ui/analytics")
    assert analytics.status_code == 200
    assert "demo" in analytics.text
    assert "pending" in analytics.text
