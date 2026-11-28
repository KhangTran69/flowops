"""Direct unit tests for the onboarding service layer (src/services/onboarding.py).

These exercise the extracted callables without going through HTTP: the item
transition state machine, the durable action executor, the run-scoped step
decisions, and the run-control idempotency protocol. The routes in
``src/routes/onboarding.py`` delegate to exactly these functions.
"""

import pytest
from fastapi import HTTPException, Response

from src import api, db  # noqa: F401  (api must be imported before routes/services)
from src.routes.onboarding import _create_onboarding_request
from src.schemas import OnboardingRequestIn
from src.services.onboarding import (  # noqa: F401  (ACTION_ADAPTER re-read in tests)
    ACTION_ADAPTER,
    ApprovalDecisionIn,
    _decide_workflow_step,
    _execute_item,
    _load_request,
    _run_action,
    _transition_item,
)


@pytest.fixture
def session_db(tmp_path):
    db.use_database(f"sqlite:///{tmp_path / 'svc-onboarding.db'}")
    db._ensure_tables()
    session = db.SessionLocal()
    yield session
    session.close()


def _request(session, request_type="onboarding", template=None):
    payload = OnboardingRequestIn(
        request_type=request_type,
        employee_name="Service Test",
        employee_email="service@example.test",
        department="IT",
        requester_id=7,
        start_date="2026-12-01",
        template=template,
    )
    return _create_onboarding_request(payload, session, actor_subject="svc-test")


def _item_by_key(items, step_key):
    return next(item for item in items if item.step_key == step_key)


def _actor():
    from src.auth import Actor

    return Actor("svc-operator", ("automation.operator",), source="local")


def test_transition_item_completes_steps_and_the_request(session_db):
    record = _request(session_db)
    # Flat template: approve the two gated steps, complete everything.
    for item in _load_request(session_db, record.id)[1]:
        if item.requires_approval:
            _transition_item(session_db, record.id, item.id, None, "approve", "svc-operator")
    for item in _load_request(session_db, record.id)[1]:
        _transition_item(session_db, record.id, item.id, None, "complete", "svc-operator")
    refreshed, items = _load_request(session_db, record.id)
    assert refreshed.status == "completed"
    assert all(item.status == "completed" for item in items)


def test_transition_item_rejects_approval_not_required(session_db):
    record = _request(session_db)
    _, items = _load_request(session_db, record.id)
    item = _item_by_key(items, "provision_laptop")  # requires_approval=False
    with pytest.raises(HTTPException) as excinfo:
        _transition_item(session_db, record.id, item.id, None, "approve", "svc-operator")
    assert excinfo.value.status_code == 409


def test_transition_item_blocks_completion_on_unmet_prerequisites(session_db):
    record = _request(session_db, template="onboarding-dag")
    _, items = _load_request(session_db, record.id)
    gated = _item_by_key(items, "grant_licenses")  # depends on create_user_account
    with pytest.raises(HTTPException) as excinfo:
        _transition_item(session_db, record.id, gated.id, None, "complete", "svc-operator")
    assert excinfo.value.status_code == 409
    assert "blocked by incomplete prerequisites" in excinfo.value.detail


def test_execute_item_runs_the_adapter_and_records_the_attempt(session_db, monkeypatch):
    record = _request(session_db)

    class Result:
        external_reference = "sim-user-1"
        detail = "simulated ok"
        output = {"user_id": "sim-user-1"}

    class Adapter:
        adapter_name = "unit-fake"

        def execute(self, action, context):
            assert context["request_id"] == record.id
            return Result()

    import src.services.onboarding as service

    monkeypatch.setattr(service, "ACTION_ADAPTER", Adapter())
    _, items = _load_request(session_db, record.id)
    item = _item_by_key(items, "provision_laptop")
    _execute_item(session_db, record.id, item.id, _actor())
    attempts = session_db.query(db.ActionExecutionRecord).all()
    assert len(attempts) == 1
    assert attempts[0].status == "succeeded"
    assert attempts[0].adapter == "unit-fake"
    refreshed, items = _load_request(session_db, record.id)
    assert _item_by_key(items, "provision_laptop").status == "completed"
    assert _item_by_key(items, "provision_laptop").output_json is not None


def test_execute_item_fails_closed_after_retries(session_db, monkeypatch):
    record = _request(session_db)

    class Adapter:
        def execute(self, action, context):
            raise RuntimeError("adapter down")

    import src.services.onboarding as service

    monkeypatch.setattr(service, "ACTION_ADAPTER", Adapter())
    notified = {}
    monkeypatch.setattr(
        service, "_notify_action_failure",
        lambda action, context, attempts, error, session: notified.update(
            action=action, attempts=attempts,
        ),
    )
    _, items = _load_request(session_db, record.id)
    item = _item_by_key(items, "provision_laptop")
    with pytest.raises(HTTPException) as excinfo:
        _execute_item(session_db, record.id, item.id, _actor())
    assert excinfo.value.status_code == 502
    assert "provision_hardware" in str(excinfo.value.detail)
    refreshed, items = _load_request(session_db, record.id)
    assert _item_by_key(items, "provision_laptop").status == "failed"
    assert notified["action"] == "provision_hardware"
    assert notified["attempts"] == 3


def test_decide_workflow_step_approves_and_rejects_duplicates(session_db):
    record = _request(session_db)
    from src import scheduler

    run = scheduler.start_run(session_db, record.id, actor_subject="svc-test")
    session_db.commit()
    _, items = _load_request(session_db, record.id)
    gated = _item_by_key(items, "create_user_account")
    out = _decide_workflow_step(
        session_db, run.id, gated.id, "approve",
        ApprovalDecisionIn(reason="service-level decision"), _actor(),
    )
    gated_status = next(i for i in out.checklist if i.step_key == "create_user_account")
    assert gated_status.status == "approved"
    _, items = _load_request(session_db, record.id)
    with pytest.raises(HTTPException) as excinfo:
        _decide_workflow_step(
            session_db, run.id, gated.id, "reject",
            ApprovalDecisionIn(reason="second decision"), _actor(),
        )
    assert excinfo.value.status_code == 409


def test_run_action_is_idempotent_per_key(session_db):
    record = _request(session_db)
    from src import scheduler

    run = scheduler.start_run(session_db, record.id, actor_subject="svc-test")
    session_db.commit()

    def act(action, key):
        return _run_action(session_db, run.id, _actor(), action, Response(), key)

    paused = act("pause", "svc-key-1")
    assert paused.status == "paused"
    replay = act("pause", "svc-key-1")
    assert replay.status == "paused"  # replayed reservation, same state
    resumed = act("resume", "svc-key-2")
    assert resumed.status == "running"
    cancelled = act("cancel", "svc-key-3")
    assert cancelled.status == "cancelled"
    with pytest.raises(HTTPException) as excinfo:
        act("resume", "svc-key-4")
    assert excinfo.value.status_code == 409  # cancelled runs cannot resume
