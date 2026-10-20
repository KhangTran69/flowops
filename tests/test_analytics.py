"""Operational analytics tests: historical reporting plus live risk signals."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src import api, db


@pytest.fixture
def client(tmp_path):
    db.use_database(f"sqlite:///{tmp_path / 'analytics.db'}")
    return TestClient(api.app)


def test_summary_reports_active_runs_and_overdue_pending_timers(client):
    now = db.utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    db.Base.metadata.create_all(bind=db.engine)
    with db.SessionLocal() as session:
        request = db.OnboardingRequestRecord(
            request_type="onboarding",
            employee_name="Ava Example",
            employee_email="ava@example.com",
            department="IT",
            requester_id=1,
            definition_key="standard",
            tenant_key="acme",
            created_at=now - timedelta(hours=3),
        )
        session.add(request)
        session.flush()
        session.add(
            db.WorkflowRunRecord(
                request_id=request.id,
                status="running",
                created_at=now - timedelta(hours=3),
                started_at=now - timedelta(hours=2),
                last_tick_at=now - timedelta(hours=2),
            )
        )
        session.add(
            db.WorkflowTimerRecord(
                request_id=request.id,
                item_id=11,
                due_at=now - timedelta(minutes=20),
                status="pending",
            )
        )
        session.commit()

    response = client.get(
        "/analytics/summary",
        params={"days": 30, "tenant": "acme"},
    )
    elapsed = (db.utcnow().replace(tzinfo=timezone.utc) - now).total_seconds()
    assert response.status_code == 200
    body = response.json()
    assert body["runs"]["active"] == 1
    # The server measures age against its own clock after setup ran, so allow
    # for the wall time this test has spent (slow CI runners took ~3s).
    assert 7200.0 - 1.0 <= body["runs"]["oldest_active_seconds"] <= 7200.0 + elapsed + 1.0
    assert body["runs"]["failure_rate"] == 0.0
    assert body["sla"]["overdue_pending"] == 1

    health = client.get(
        "/analytics/health",
        params={"tenant": "acme", "stuck_minutes": 60},
    )
    assert health.status_code == 200
    assert health.json()["stuck_count"] == 1
    assert health.json()["stuck_runs"][0]["request_id"] > 0

    assert client.get(
        "/analytics/health", params={"tenant": "other"}
    ).json()["stuck_count"] == 0


def test_analytics_page_renders_operational_metrics(client):
    response = client.get("/ui/analytics")
    assert response.status_code == 200
    assert "active runs" in response.text
    assert "SLA items at risk" in response.text
    assert "Workflow health" in response.text
    assert 'name="stuck_minutes"' in response.text
    assert "Everything is moving" in response.text
