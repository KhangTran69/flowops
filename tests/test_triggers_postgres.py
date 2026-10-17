"""PostgreSQL-specific tests for event-driven triggers (v1.4)."""

import os

import pytest

from src import db, definitions, jobs, triggers, worker

# Skip if PostgreSQL not available
pytestmark = pytest.mark.skipif(
    not os.environ.get("FLOWOPS_TEST_POSTGRES_URL"),
    reason="FLOWOPS_TEST_POSTGRES_URL not set",
)


@pytest.fixture
def pg_session():
    url = os.environ["FLOWOPS_TEST_POSTGRES_URL"]
    db.use_database(url)
    db.Base.metadata.create_all(bind=db.engine)
    session = db.SessionLocal()
    try:
        yield session
        session.rollback()
    finally:
        session.close()


def _create_event_source(session, key="pg-test-source"):
    source = db.EventSourceRecord(
        key=key,
        name="PG Test Source",
        secret="pg-secret-key-12345",
        active=True,
        created_at=db.utcnow(),
    )
    session.add(source)
    session.commit()
    return source


def _create_definition(session, key="pg-test-def"):
    definition = definitions.create_definition(
        session,
        key=key,
        name="PG Test Definition",
        request_type="onboarding",
        steps=[
            {
                "key": "step1",
                "step": "Step 1",
                "queue": "it",
                "requires_approval": False,
                "action": "none",
                "depends_on": [],
            }
        ],
    )
    definitions.publish_version(session, key, 1)
    session.commit()
    return definition


def _create_trigger(
    session,
    key="pg-test-trigger",
    source_key="pg-test-source",
    target_type="onboarding_request",
    definition_key=None,
):
    import json

    trigger = db.TriggerRecord(
        key=key,
        name="PG Test Trigger",
        source_key=source_key,
        event_type="user.created",
        target_type=target_type,
        definition_key=definition_key,
        mapping_json=json.dumps(
            {
                "employee_name": "event.data.name",
                "employee_email": "event.data.email",
                "department": "event.data.department",
                "requester_id": 1,
            },
            separators=(",", ":"),
        ),
        schedule_kind="webhook",
        active=True,
        created_at=db.utcnow(),
        updated_at=db.utcnow(),
    )
    session.add(trigger)
    session.commit()
    return trigger


def test_pg_delivery_deduplication(pg_session):
    """PostgreSQL delivery deduplication works with unique constraint."""
    trigger = _create_trigger(pg_session)
    event_id = "pg-evt-12345"
    payload = {"test": "data"}

    delivery1 = triggers.record_delivery(pg_session, trigger.key, event_id, payload)
    delivery2 = triggers.record_delivery(pg_session, trigger.key, event_id, payload)

    assert delivery1.id == delivery2.id


def test_pg_scheduler_tick_fires_due_triggers(pg_session):
    """PostgreSQL scheduler ticks fire due triggers."""
    from datetime import timedelta

    trigger = _create_trigger(pg_session)
    trigger.schedule_kind = "interval"
    trigger.interval_seconds = 60
    trigger.last_fired_at = db.utcnow() - timedelta(seconds=120)
    pg_session.commit()

    ticks = triggers.tick_scheduler(pg_session)
    assert len(ticks) == 1
    assert ticks[0].trigger_key == "pg-test-trigger"


def test_pg_trigger_worker_creates_onboarding_request(pg_session, monkeypatch):
    """PostgreSQL trigger worker creates onboarding request."""
    from src.actions import ActionResult

    _create_event_source(pg_session)
    definition = _create_definition(pg_session)
    trigger = _create_trigger(pg_session, definition_key="pg-test-def")

    # Create delivery
    delivery = triggers.record_delivery(
        pg_session,
        trigger.key,
        "pg-evt-123",
        {"data": {"name": "Alice", "email": "alice@example.com", "department": "IT"}},
        status="queued",
    )

    # Enqueue job
    job = jobs.enqueue_job(
        pg_session,
        job_type=jobs.JOB_TRIGGER_EXECUTE,
        payload={
            "delivery_id": delivery.id,
            "trigger_key": trigger.key,
            "event_id": "pg-evt-123",
            "event_payload": {"data": {"name": "Alice", "email": "alice@example.com", "department": "IT"}},
        },
    )
    pg_session.commit()

    # Process job
    class FakeAdapter:
        def execute(self, action, context):
            return ActionResult(action=action, detail="fake")

    monkeypatch.setattr(worker, "ACTION_ADAPTER", FakeAdapter())

    processed = worker.run_once(worker_id="test-worker")
    assert processed is not None
    assert processed.status == "succeeded"

    # Verify request created
    from sqlalchemy import select

    request = pg_session.execute(
        select(db.OnboardingRequestRecord).where(
            db.OnboardingRequestRecord.employee_name == "Trigger Employee"
        )
    ).scalar_one_or_none()
    assert request is not None
    assert request.definition_key == "pg-test-def"


def test_pg_concurrent_scheduler_prevents_double_fire(pg_session):
    """PostgreSQL concurrent schedulers prevent double-firing."""
    from datetime import timedelta

    trigger = _create_trigger(pg_session)
    trigger.schedule_kind = "interval"
    trigger.interval_seconds = 60
    trigger.last_fired_at = db.utcnow() - timedelta(seconds=120)
    pg_session.commit()

    # Simulate two concurrent schedulers
    ticks1 = triggers.tick_scheduler(pg_session)
    ticks2 = triggers.tick_scheduler(pg_session)

    assert len(ticks1) == 1
    assert len(ticks2) == 0
