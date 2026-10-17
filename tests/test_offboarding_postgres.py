"""PostgreSQL coverage for the offboarding completion ledger."""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src import api, db

POSTGRES_URL = os.environ.get("FLOWOPS_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL, reason="FLOWOPS_TEST_POSTGRES_URL is not set"
)

OFFBOARDING = {
    "request_type": "offboarding",
    "employee_name": "Jane Doe",
    "employee_email": "jane@example.com",
    "department": "Sales",
    "requester_id": 3,
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


def test_postgres_offboarding_claim_and_replay(client, monkeypatch):
    calls = []

    def fake(step_key, email):
        calls.append(step_key)
        return {"simulated": True, "action": step_key, "target": email}

    monkeypatch.setattr(api, "offboarding_executor", fake)
    created = client.post("/onboarding-requests", json=OFFBOARDING)
    assert created.status_code == 201
    checklist_id = created.json()["id"]
    path = f"/offboarding-requests/{checklist_id}/steps/recover_laptop/complete"

    first = client.post(path)
    assert first.status_code == 201
    replay = client.post(path)
    assert replay.status_code == 200
    assert replay.headers["X-Offboarding-Replayed"] == "true"
    assert len(calls) == 1

    session = db.SessionLocal()
    try:
        rows = session.execute(
            select(db.OffboardingCompletionRecord).where(
                db.OffboardingCompletionRecord.checklist_id == checklist_id
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "completed"
    finally:
        session.close()
