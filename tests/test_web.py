"""Tests for the server-rendered operator UI (Phase 1)."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src import api, db

TICKET = {
    "userId": 7,
    "id": 42,
    "title": "Laptop will not start",
    "completed": False,
}

ONBOARDING = {
    "request_type": "onboarding",
    "employee_name": "Jane Doe",
    "employee_email": "jane@example.com",
    "department": "Sales",
    "requester_id": 3,
    "start_date": "2026-11-02",
}


@pytest.fixture
def client(tmp_path):
    db.use_database(f"sqlite:///{tmp_path}/web.db")
    return TestClient(api.app)


def test_landing_renders_design_and_live_counts(client):
    assert client.post("/tickets", json=TICKET).status_code == 201
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    page = response.text
    assert "FlowOps" in page
    assert "Open dashboard" in page
    assert "API healthy" in page
    assert "Quick start" in page
    assert "/static/css/app.css" in page
    assert "/ui/onboarding-requests" in page


def test_static_assets_are_served(client):
    css = client.get("/static/css/app.css")
    js = client.get("/static/js/app.js")
    assert css.status_code == 200
    assert js.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert "text/javascript" in js.headers["content-type"]


def test_knowledge_base_ui_renders_articles_instead_of_json(client):
    response = client.get("/ui/knowledge-base", params={"query": "kb-hardware"})
    assert response.status_code == 200
    assert "Knowledge base" in response.text
    assert "Triage laptop, monitor, and peripheral faults" in response.text
    assert "Recommended steps" in response.text
    assert "application/json" not in response.headers.get("content-type", "")


def test_dashboard_template_and_empty_states(client):
    page = client.get("/dashboard").text
    assert "FlowOps monitoring dashboard" in page
    assert "No workflow runs yet." in page
    assert "No pending deliveries." in page
    assert "No dead letters." in page
    assert "auto-refresh" in page


def test_dashboard_shows_filtered_runs(client):
    client.post("/tickets", json=TICKET)
    success = client.get("/dashboard", params={"status": "success"})
    failure = client.get("/dashboard", params={"status": "failure"})
    assert "ticket.created" in success.text
    assert "No workflow runs yet." in failure.text


def test_onboarding_list_and_detail_pages(client):
    created = client.post("/onboarding-requests", json=ONBOARDING)
    assert created.status_code == 201
    request_id = created.json()["id"]

    listing = client.get("/ui/onboarding-requests")
    assert listing.status_code == 200
    assert "Jane Doe" in listing.text
    assert f"/ui/onboarding-requests/{request_id}" in listing.text

    detail = client.get(f"/ui/onboarding-requests/{request_id}")
    assert detail.status_code == 200
    assert "Jane Doe" in detail.text
    assert "Checklist" in detail.text
    assert f"/onboarding-requests/{request_id}/items/" in detail.text


def test_onboarding_detail_escapes_untrusted_data(client):
    created = client.post(
        "/onboarding-requests",
        json={**ONBOARDING, "employee_name": "<script>alert(1)</script>"},
    )
    request_id = created.json()["id"]
    page = client.get(f"/ui/onboarding-requests/{request_id}").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<script>alert(1)</script>" not in page


def test_onboarding_missing_request_renders_404_page(client):
    response = client.get("/ui/onboarding-requests/999")
    assert response.status_code == 404
    assert "Page not found" in response.text


def test_notifications_page_lists_pending_and_dead_letters(client):
    client.post("/tickets", json=TICKET)
    session = db.SessionLocal()
    try:
        session.add(
            db.NotificationOutboxRecord(
                url="https://hooks.example/dead",
                payload_json='{"event":"ticket.created"}',
                ticket_id="42",
                status="failed",
                attempts=5,
                last_error="boom",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    finally:
        session.close()

    page = client.get("/ui/notifications").text
    assert "Notification outbox" in page
    assert "Dead letters (1)" in page
    assert "hooks.example/dead" in page
    assert "/requeue" in page


def test_unknown_ui_path_renders_friendly_404(client):
    response = client.get("/ui/does-not-exist")
    assert response.status_code == 404
    assert "Page not found" in response.text


def test_jobs_page_renders(client):
    response = client.get("/ui/workflow-jobs")
    assert response.status_code == 200
    assert "Workflow jobs" in response.text


def test_audit_page_renders_actor_rows(client, monkeypatch):
    monkeypatch.setenv("FLOWOPS_AUTH_REQUIRED", "true")
    monkeypatch.setenv("FLOWOPS_DEV_BEARER_TOKEN", "dev-secret")
    payload = {"userId": 7, "id": 42, "title": "Laptop will not start", "completed": False}
    assert client.post(
        "/tickets", json=payload, headers={"Authorization": "Bearer dev-secret"}
    ).status_code == 201
    page = client.get("/ui/audit").text
    assert "Audit trail" in page
    assert "configured-bearer" in page
    assert "ticket.created" in page


def test_job_detail_page_renders(client):
    created = client.post("/onboarding-requests", json=ONBOARDING)
    request_id = created.json()["id"]
    item_id = next(
        item["id"] for item in created.json()["checklist"] if not item["requires_approval"]
    )
    job = client.post(
        f"/onboarding-requests/{request_id}/items/{item_id}/jobs"
    ).json()
    page = client.get(f"/ui/workflow-jobs/{job['id']}")
    assert page.status_code == 200
    assert "Job state" in page.text
    assert "What was requested" in page.text
    assert "Item Id" in page.text
    assert "View raw data" in page.text
    assert f"/workflow-jobs/{job['id']}/cancel" in page.text


def test_job_detail_missing_renders_404(client):
    response = client.get("/ui/workflow-jobs/999")
    assert response.status_code == 404
    assert "Page not found" in response.text


def test_offboarding_detail_uses_ledger_completion_endpoint(client):
    created = client.post(
        "/onboarding-requests",
        json={**ONBOARDING, "request_type": "offboarding"},
    )
    assert created.status_code == 201
    body = created.json()
    page = client.get(f"/ui/onboarding-requests/{body['id']}").text
    assert f"/offboarding-requests/{body['id']}/steps/recover_laptop/complete" in page
    assert f"/offboarding-requests/{body['id']}/steps/disable_user_account/complete" not in page


def test_dashboard_shows_offboarding_progress(client):
    created = client.post(
        "/onboarding-requests",
        json={**ONBOARDING, "request_type": "offboarding", "employee_name": "Leaving Person"},
    )
    assert created.status_code == 201
    page = client.get("/dashboard").text
    assert "Offboarding progress" in page
    assert "Leaving Person" in page
