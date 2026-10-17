"""Offline tests for the v1.1 DAG engine and ChatOps approvals.

Every test runs against SQLite in a temporary directory and never touches the
network: Slack/Teams delivery is simulated by calling the same signed webhook
endpoints a real ChatOps button would call.
"""

import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from src import api, chatops, db, jobs, onboarding, worker, workflow
from src.actions import ActionResult

ONBOARDING = {
    "request_type": "onboarding",
    "employee_name": "Ada Lovelace",
    "employee_email": "ada@example.com",
    "department": "R&D",
    "requester_id": 3,
    "start_date": "2026-11-02",
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWOPS_CHATOPS_SECRET", secrets.token_urlsafe(24))
    monkeypatch.setenv("FLOWOPS_RATE_LIMIT_PER_MINUTE", "0")
    db.use_database(f"sqlite:///{tmp_path}/dag.db")
    return TestClient(api.app)


def _create(client, **overrides):
    payload = {**ONBOARDING, **overrides}
    response = client.post("/onboarding-requests", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _by_key(body):
    return {item["step_key"]: item for item in body["checklist"]}


def _statuses(body):
    return {item["step_key"]: item["status"] for item in body["checklist"]}


def _approve_url(link, source="webhook"):
    return f"/webhooks/actions/approve?token={quote(link, safe='')}&source={source}"


def _reject_url(link, reason=None, source="webhook"):
    url = f"/webhooks/actions/reject?token={quote(link, safe='')}&source={source}"
    if reason is not None:
        url = f"{url}&reason={quote(reason, safe='')}"
    return url


def _state(key, status, *, deps=(), gated=False):
    return onboarding.StepState(
        key=key, status=status, requires_approval=gated, depends_on=tuple(deps)
    )


class _CapturingAdapter:
    """Adapter double that records the context and returns structured output."""

    def __init__(self, output=None):
        self.contexts = {}
        self.output = output or {"user_id": "u-42", "mailbox_guid": "m-7"}

    def execute(self, action, context):
        self.contexts[action] = context
        return ActionResult(action=action, detail=f"captured {action}", output=dict(self.output))


# ---------------------------------------------------------------------------
# Pure graph evaluation


def test_is_step_ready_requires_every_prerequisite():
    states = {
        "gate": _state("gate", "completed"),
        "other": _state("other", "pending"),
        "dependent": _state("dependent", "blocked", deps=("gate", "other")),
    }
    assert onboarding.is_step_ready(states["dependent"], states) is False
    states["other"] = _state("other", "completed")
    assert onboarding.is_step_ready(states["dependent"], states) is True


def test_resolve_promotes_only_when_all_prerequisites_complete():
    states = {
        "a": _state("a", "completed"),
        "b": _state("b", "blocked", deps=("a",)),
        "c": _state("c", "blocked", deps=("a", "x")),
        "x": _state("x", "pending"),
    }
    transitions = {t.key: t.to_status for t in onboarding.resolve_blocked_steps(states)}
    assert transitions == {"b": onboarding.STATUS_PENDING}


def test_resolve_promotes_pre_approved_gated_step_to_approved():
    states = {
        "a": _state("a", "completed"),
        "b": _state("b", "blocked", deps=("a",), gated=True),
    }
    (transition,) = onboarding.resolve_blocked_steps(states, pre_approved={"b"})
    assert transition.to_status == onboarding.STATUS_APPROVED


def test_cascade_propagates_through_a_chain():
    states = {
        "a": _state("a", "rejected"),
        "b": _state("b", "blocked", deps=("a",)),
        "c": _state("c", "blocked", deps=("b",)),
    }
    transitions = {t.key: t.to_status for t in onboarding.resolve_blocked_steps(states)}
    assert transitions == {
        "b": onboarding.STATUS_CASCADE_BLOCKED,
        "c": onboarding.STATUS_CASCADE_BLOCKED,
    }


def test_missing_prerequisite_fails_closed():
    states = {"c": _state("c", "blocked", deps=("ghost",))}
    (transition,) = onboarding.resolve_blocked_steps(states)
    assert transition.to_status == onboarding.STATUS_CASCADE_BLOCKED


def test_validate_graph_rejects_cycles_unknown_keys_and_duplicates():
    with pytest.raises(onboarding.WorkflowGraphError, match="cycle"):
        onboarding.validate_graph(
            [{"key": "a", "depends_on": ["b"]}, {"key": "b", "depends_on": ["a"]}]
        )
    with pytest.raises(onboarding.WorkflowGraphError, match="unknown step"):
        onboarding.validate_graph([{"key": "a", "depends_on": ["nope"]}])
    with pytest.raises(onboarding.WorkflowGraphError, match="Duplicate"):
        onboarding.validate_graph([{"key": "a"}, {"key": "a"}])
    with pytest.raises(onboarding.WorkflowGraphError, match="itself"):
        onboarding.validate_graph([{"key": "a", "depends_on": ["a"]}])


def test_topological_order_puts_prerequisites_first():
    steps = onboarding.checklist_for("onboarding", template="onboarding-dag")
    order = onboarding.topological_order(steps)
    assert order.index("create_user_account") < order.index("grant_licenses")
    assert order.index("create_user_account") < order.index("configure_mailbox")


# ---------------------------------------------------------------------------
# Templates and creation


def test_flat_template_still_creates_immediately_actionable_steps(client):
    body = _create(client)
    assert _statuses(body) == {key: "pending" for key in _by_key(body)}
    assert all(item["depends_on"] == [] for item in body["checklist"])


def test_dag_template_blocks_steps_with_prerequisites(client):
    body = _create(client, template="onboarding-dag")
    statuses = _statuses(body)
    assert statuses["create_user_account"] == "pending"
    assert statuses["provision_laptop"] == "pending"
    assert statuses["grant_licenses"] == "blocked"
    assert statuses["configure_mailbox"] == "blocked"
    assert _by_key(body)["configure_mailbox"]["depends_on"] == ["create_user_account"]


def test_unknown_template_is_rejected(client):
    response = client.post("/onboarding-requests", json={**ONBOARDING, "template": "nope"})
    assert response.status_code == 422


def test_offboarding_dag_is_offered_and_blocked(client):
    """The ledger path now enforces the same prerequisites as onboarding."""
    body = {
        **ONBOARDING,
        "request_type": "offboarding",
        "template": "offboarding-dag",
    }
    response = client.post("/onboarding-requests", json=body)
    assert response.status_code == 201
    statuses = {item["step_key"]: item["status"] for item in response.json()["checklist"]}
    assert statuses["disable_user_account"] == "pending"
    assert statuses["revoke_licenses"] == "blocked"
    assert statuses["archive_mailbox"] == "blocked"


# ---------------------------------------------------------------------------
# Dependency gates and unblocking


def test_execute_blocked_step_returns_409_with_prerequisite_names(client):
    body = _create(client, template="onboarding-dag")
    item = _by_key(body)["grant_licenses"]
    response = client.post(f"/onboarding-requests/{body['id']}/items/{item['id']}/execute")
    assert response.status_code == 409
    assert "create_user_account" in response.json()["detail"]


def test_enqueue_blocked_step_is_rejected(client):
    body = _create(client, template="onboarding-dag")
    item = _by_key(body)["configure_mailbox"]
    response = client.post(f"/onboarding-requests/{body['id']}/items/{item['id']}/jobs")
    assert response.status_code == 409


def test_completing_prerequisite_unblocks_dependents(client):
    body = _create(client, template="onboarding-dag")
    request_id = body["id"]
    items = _by_key(body)
    client.post(f"/onboarding-requests/{request_id}/items/{items['create_user_account']['id']}/approve")
    completed = client.post(
        f"/onboarding-requests/{request_id}/items/{items['create_user_account']['id']}/execute"
    )
    assert completed.status_code == 200
    statuses = _statuses(completed.json())
    assert statuses["grant_licenses"] == "pending"
    assert statuses["configure_mailbox"] == "pending"
    logs = [log["action"] for log in client.get("/execution-logs").json()]
    assert "onboarding.item.unblocked" in logs


def test_independent_steps_execute_concurrently(client):
    body = _create(client, template="onboarding-dag")
    request_id = body["id"]
    items = _by_key(body)
    client.post(f"/onboarding-requests/{request_id}/items/{items['create_user_account']['id']}/approve")
    targets = [items["create_user_account"]["id"], items["provision_laptop"]["id"]]

    def run(item_id):
        return client.post(
            f"/onboarding-requests/{request_id}/items/{item_id}/execute"
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(run, targets))
    assert statuses == [200, 200]
    final = _statuses(client.get(f"/onboarding-requests/{request_id}").json())
    assert final["create_user_account"] == "completed"
    assert final["provision_laptop"] == "completed"


def test_terminal_failure_cascades_to_dependents(client, monkeypatch):
    class _FailingAdapter:
        def execute(self, action, context):
            raise RuntimeError("graph failure")

    monkeypatch.setattr(api, "ACTION_ADAPTER", _FailingAdapter())
    monkeypatch.setenv("FLOWOPS_ACTION_RETRIES", '{"create_user_account": 1}')
    body = _create(client, template="onboarding-dag")
    request_id = body["id"]
    items = _by_key(body)
    client.post(f"/onboarding-requests/{request_id}/items/{items['create_user_account']['id']}/approve")
    failed = client.post(
        f"/onboarding-requests/{request_id}/items/{items['create_user_account']['id']}/execute"
    )
    assert failed.status_code == 502
    statuses = _statuses(client.get(f"/onboarding-requests/{request_id}").json())
    assert statuses["create_user_account"] == "failed"
    assert statuses["grant_licenses"] == "cascade_blocked"
    assert statuses["configure_mailbox"] == "cascade_blocked"


# ---------------------------------------------------------------------------
# Output context piping


def test_prerequisite_outputs_are_piped_into_downstream_context(client, monkeypatch):
    adapter = _CapturingAdapter()
    monkeypatch.setattr(api, "ACTION_ADAPTER", adapter)
    body = _create(client, template="onboarding-dag")
    request_id = body["id"]
    items = _by_key(body)

    client.post(f"/onboarding-requests/{request_id}/items/{items['create_user_account']['id']}/approve")
    gate = client.post(
        f"/onboarding-requests/{request_id}/items/{items['create_user_account']['id']}/execute"
    )
    assert gate.status_code == 200
    gate_item = next(i for i in gate.json()["checklist"] if i["step_key"] == "create_user_account")
    assert gate_item["output"] == {"user_id": "u-42", "mailbox_guid": "m-7"}

    mailbox = client.post(
        f"/onboarding-requests/{request_id}/items/{items['configure_mailbox']['id']}/execute"
    )
    assert mailbox.status_code == 200
    context = adapter.contexts["configure_email_mailbox"]
    assert context["prerequisite_outputs"]["create_user_account"]["user_id"] == "u-42"
    assert context["upstream_outputs"]["mailbox_guid"] == "m-7"


def test_step_output_is_persisted_for_later_reads(client, monkeypatch):
    monkeypatch.setattr(api, "ACTION_ADAPTER", _CapturingAdapter())
    body = _create(client, template="onboarding-dag")
    request_id = body["id"]
    items = _by_key(body)
    client.post(f"/onboarding-requests/{request_id}/items/{items['create_user_account']['id']}/approve")
    client.post(f"/onboarding-requests/{request_id}/items/{items['create_user_account']['id']}/execute")

    session = db.SessionLocal()
    try:
        record = session.get(db.ChecklistItemRecord, items["create_user_account"]["id"])
        assert workflow.step_output(record)["user_id"] == "u-42"
    finally:
        session.close()


def test_worker_injects_prerequisite_outputs(client, monkeypatch):
    adapter = _CapturingAdapter()
    monkeypatch.setattr(api, "ACTION_ADAPTER", adapter)
    body = _create(client, template="onboarding-dag")
    request_id = body["id"]
    items = _by_key(body)
    client.post(f"/onboarding-requests/{request_id}/items/{items['create_user_account']['id']}/approve")
    client.post(f"/onboarding-requests/{request_id}/items/{items['create_user_account']['id']}/execute")

    monkeypatch.setattr(worker, "ACTION_ADAPTER", adapter)
    session = db.SessionLocal()
    try:
        jobs.enqueue_job(
            session,
            job_type=jobs.JOB_ONBOARDING_ITEM,
            payload={
                "request_id": request_id,
                "item_id": items["configure_mailbox"]["id"],
                "action": "configure_email_mailbox",
            },
            idempotency_key="worker-dag-1",
        )
        session.commit()
    finally:
        session.close()

    finished = worker.run_once(worker_id="dag-test")
    assert finished is not None and finished.status == "succeeded"
    context = adapter.contexts["configure_email_mailbox"]
    assert context["prerequisite_outputs"]["create_user_account"]["user_id"] == "u-42"


def test_worker_dead_letters_a_blocked_item(client):
    body = _create(client, template="onboarding-dag")
    request_id = body["id"]
    items = _by_key(body)
    session = db.SessionLocal()
    try:
        jobs.enqueue_job(
            session,
            job_type=jobs.JOB_ONBOARDING_ITEM,
            payload={
                "request_id": request_id,
                "item_id": items["grant_licenses"]["id"],
                "action": "grant_software_licenses",
            },
            idempotency_key="worker-dag-blocked",
        )
        session.commit()
    finally:
        session.close()

    finished = worker.run_once(worker_id="dag-test")
    assert finished is not None
    assert finished.status == "failed"
    assert "blocked" in (finished.last_error or "").lower()


# ---------------------------------------------------------------------------
# ChatOps tokens


def test_token_roundtrip_binds_request_item_and_action(client):
    claims = chatops.verify_token(chatops.generate_token(11, 22, "approve"))
    assert (claims.request_id, claims.item_id, claims.action) == (11, 22, "approve")
    assert claims.jti and claims.expires_at > int(time.time())


def test_corrupted_signature_is_rejected(client):
    signed = chatops.generate_token(11, 22, "approve")
    with pytest.raises(chatops.ChatOpsError, match="signature"):
        chatops.verify_token(signed[:-2] + ("aa" if signed[-2:] != "aa" else "bb"))


def test_expired_link_is_rejected(client):
    stale = chatops.generate_token(11, 22, "approve", ttl_seconds=-1)
    with pytest.raises(chatops.ChatOpsError, match="expired"):
        chatops.verify_token(stale)


def test_link_is_bound_to_one_action(client):
    body = _create(client, template="onboarding-dag")
    items = _by_key(body)
    approve_link = chatops.generate_token(body["id"], items["create_user_account"]["id"], "approve")
    response = client.post(_reject_url(approve_link))
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# ChatOps webhook endpoints


def _gated_item(client):
    body = _create(client, template="onboarding-dag")
    return body["id"], _by_key(body)["create_user_account"]


def test_chatops_approve_updates_status_and_audits_actor(client):
    request_id, item = _gated_item(client)
    link = chatops.generate_token(request_id, item["id"], "approve")
    response = client.post(_approve_url(link, source="slack"))
    assert response.status_code == 200, response.text
    assert response.json()["actor"] == "chatops:slack"
    assert response.json()["status"] == "approved"

    item_now = _by_key(client.get(f"/onboarding-requests/{request_id}").json())[
        "create_user_account"
    ]
    assert item_now["status"] == "approved"
    logs = client.get("/execution-logs").json()
    chatops_logs = [log for log in logs if log["action"] == "onboarding.item.chatops_approved"]
    assert chatops_logs and chatops_logs[0]["actor_subject"] == "chatops:slack"


def test_chatops_reject_records_reason_and_cascades(client):
    request_id, item = _gated_item(client)
    link = chatops.generate_token(request_id, item["id"], "reject")
    response = client.post(_reject_url(link, reason="duplicate hire record", source="email"))
    assert response.status_code == 200, response.text
    assert response.json()["actor"] == "chatops:email"
    statuses = _statuses(client.get(f"/onboarding-requests/{request_id}").json())
    assert statuses["create_user_account"] == "rejected"
    assert statuses["grant_licenses"] == "cascade_blocked"
    assert statuses["configure_mailbox"] == "cascade_blocked"
    logs = client.get("/execution-logs").json()
    assert any("duplicate hire record" in (log["detail"] or "") for log in logs)


def test_chatops_link_cannot_be_replayed(client):
    request_id, item = _gated_item(client)
    link = chatops.generate_token(request_id, item["id"], "approve")
    assert client.post(_approve_url(link)).status_code == 200
    replay = client.post(_approve_url(link))
    assert replay.status_code == 409
    session = db.SessionLocal()
    try:
        uses = session.query(db.ChatOpsTokenUseRecord).all()
        assert len(uses) == 1
    finally:
        session.close()


def test_chatops_expired_link_returns_401(client):
    request_id, item = _gated_item(client)
    stale = chatops.generate_token(request_id, item["id"], "approve", ttl_seconds=-5)
    response = client.post(_approve_url(stale))
    assert response.status_code == 401
    assert "expired" in response.json()["detail"].lower()


def test_chatops_requires_configuration(client, monkeypatch):
    request_id, item = _gated_item(client)
    link = chatops.generate_token(request_id, item["id"], "approve")
    monkeypatch.delenv("FLOWOPS_CHATOPS_SECRET", raising=False)
    response = client.post(_approve_url(link))
    assert response.status_code == 503


def test_chatops_approve_on_blocked_step_is_rejected(client):
    body = _create(client, template="onboarding-dag")
    blocked_item = _by_key(body)["grant_licenses"]
    link = chatops.generate_token(body["id"], blocked_item["id"], "approve")
    response = client.post(_approve_url(link))
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# Operator UI


def test_ui_renders_dependencies_blocker_badge_and_output(client, monkeypatch):
    monkeypatch.setattr(api, "ACTION_ADAPTER", _CapturingAdapter())
    body = _create(client, template="onboarding-dag")
    request_id = body["id"]
    items = _by_key(body)
    page = client.get(f"/ui/onboarding-requests/{request_id}").text
    assert "Depends on: Create user account" in page
    assert "Waiting on prerequisites" in page

    client.post(f"/onboarding-requests/{request_id}/items/{items['create_user_account']['id']}/approve")
    client.post(f"/onboarding-requests/{request_id}/items/{items['create_user_account']['id']}/execute")
    page = client.get(f"/ui/onboarding-requests/{request_id}").text
    assert "Step output" in page
    assert "u-42" in page
