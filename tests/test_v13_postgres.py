"""PostgreSQL coverage for v1.3 definitions and SLA timers."""

import os

import pytest
from fastapi.testclient import TestClient

from src import api, db

POSTGRES_URL = os.environ.get("FLOWOPS_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL, reason="FLOWOPS_TEST_POSTGRES_URL is not set"
)

STEPS = [
    {
        "key": "independent",
        "step": "Independent",
        "queue": "it-service-desk",
        "requires_approval": False,
        "action": "none",
        "depends_on": [],
        "join": "all",
        "when": [],
        "sla_minutes": 5,
    }
]


@pytest.fixture
def client():
    db.use_database(POSTGRES_URL)
    db.Base.metadata.drop_all(bind=db.engine)
    db.Base.metadata.create_all(bind=db.engine)
    try:
        yield TestClient(api.app)
    finally:
        db.Base.metadata.drop_all(bind=db.engine)


def test_postgres_definition_publish_pin_and_timer(client):
    created = client.post(
        "/workflow-definitions",
        json={
            "key": "pg-definition",
            "name": "PG definition",
            "request_type": "onboarding",
            "steps": STEPS,
        },
    )
    assert created.status_code == 200
    published = client.post(
        "/workflow-definitions/pg-definition/publish", json={"version": 1}
    )
    assert published.status_code == 200
    request = client.post(
        "/onboarding-requests",
        json={
            "request_type": "onboarding",
            "employee_name": "Dana",
            "employee_email": "dana@example.com",
            "department": "Eng",
            "requester_id": 1,
            "definition_key": "pg-definition",
        },
    )
    assert request.status_code == 201
    assert request.json()["definition_version"] == 1
    session = db.SessionLocal()
    try:
        assert session.query(db.WorkflowTimerRecord).count() == 1
    finally:
        session.close()
