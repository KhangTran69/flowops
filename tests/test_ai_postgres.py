"""PostgreSQL coverage for AI triage and approval."""

import os

import pytest
from fastapi.testclient import TestClient

from src import api, db

POSTGRES_URL = os.environ.get("FLOWOPS_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL, reason="FLOWOPS_TEST_POSTGRES_URL is not set"
)

TICKET = {
    "userId": 7,
    "id": 42,
    "title": "VPN outage - all users cannot work",
    "completed": False,
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


def test_postgres_ai_triage_and_apply(client):
    created = client.post("/tickets", json=TICKET)
    assert created.status_code == 201
    session = db.SessionLocal()
    try:
        record = session.get(db.TicketRecord, "42")
        record.queue = "it-service-desk"
        record.priority = "normal"
        session.commit()
    finally:
        session.close()

    triage = client.post("/tickets/42/ai-triage")
    assert triage.status_code == 200
    ids = [
        row["id"]
        for row in triage.json()["recommendations"]
        if row["applicable"]
    ]
    applied = client.post(
        "/tickets/42/ai-recommendations/apply",
        json={"recommendation_ids": ids},
    )
    assert applied.status_code == 200
    assert applied.json()["ticket"]["queue"] == "it-network"
    assert applied.json()["ticket"]["priority"] == "urgent"

    logs = client.get("/execution-logs", params={"actor": "local-development"}).json()
    ai_logs = [row for row in logs if row["action"].startswith("ai.")]
    assert ai_logs
    assert all(row["correlation_id"] for row in ai_logs)
