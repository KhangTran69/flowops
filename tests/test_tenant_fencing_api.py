"""Tenant fencing regressions for the core ticket and onboarding APIs."""
import pytest
from fastapi.testclient import TestClient

from src import api, chatops, connectwise, db
from src.auth import Actor
from src.deps import _actor_from_header

ONBOARDING = {
    "request_type": "onboarding",
    "employee_name": "Jane Doe",
    "employee_email": "jane@example.com",
    "department": "Sales",
    "requester_id": 3,
    "start_date": "2026-11-02",
}


@pytest.fixture
def fencing_client(tmp_path, monkeypatch):
    db.use_database(f"sqlite:///{tmp_path}/tenancy.db")
    db._ensure_tables()
    setup = db.SessionLocal()
    try:
        setup.add(db.TenantRecord(key="tenant-a", name="Tenant A", active=True))
        setup.add(db.TenantRecord(key="tenant-b", name="Tenant B", active=True))
        setup.commit()
    finally:
        setup.close()
    monkeypatch.setenv("FLOWOPS_ENVIRONMENT", "development")
    actors = {"actor": Actor("ops-a", ("automation.operator",), source="bearer", tenant_key="tenant-a")}
    api.app.dependency_overrides[_actor_from_header] = lambda: actors["actor"]
    with TestClient(api.app) as client:
        def set_actor(value):
            actors["actor"] = value
        yield client, set_actor
    api.app.dependency_overrides.pop(_actor_from_header, None)


def _tenant_actor(key):
    return Actor(f"ops-{key}", ("automation.operator",), source="bearer", tenant_key=key)


def _local_actor():
    return Actor("local-development", ("automation.operator", "automation.admin"), source="local")


def test_tenant_actor_sees_only_its_tenant_tickets(fencing_client):
    client, set_actor = fencing_client
    set_actor(_tenant_actor("tenant-a"))
    created = client.post("/tickets", json={
        "id": 1001, "userId": 77, "title": "Email broken for Tenant A",
        "completed": False, "tenant": "tenant-a",
    })
    assert created.status_code == 201, created.text
    ticket_id = created.json()["ticket_id"]

    set_actor(_tenant_actor("tenant-b"))
    assert client.get("/tickets").json() == []
    assert client.get(f"/tickets/{ticket_id}").status_code == 404

    set_actor(_tenant_actor("tenant-a"))
    assert [row["ticket_id"] for row in client.get("/tickets").json()] == [ticket_id]
    assert client.get(f"/tickets/{ticket_id}").status_code == 200


def test_foreign_ticket_lookup_is_denied_without_mutation(fencing_client):
    client, set_actor = fencing_client
    set_actor(_tenant_actor("tenant-a"))
    created = client.post("/tickets", json={
        "id": 1010, "userId": 77, "title": "Tenant A VPN issue",
        "completed": False,
    })
    assert created.status_code == 201, created.text
    ticket_id = created.json()["ticket_id"]
    before = client.get(f"/tickets/{ticket_id}")
    assert before.status_code == 200
    before_representation = before.json()

    session = db.SessionLocal()
    try:
        record = session.get(db.TicketRecord, ticket_id)
        assert record.tenant_key == "tenant-a"
        before_row = (record.tenant_key, record.title, record.status, record.priority, record.queue)
    finally:
        session.close()

    set_actor(_tenant_actor("tenant-b"))
    assert client.get(f"/tickets/{ticket_id}").status_code == 404

    set_actor(_tenant_actor("tenant-a"))
    after = client.get(f"/tickets/{ticket_id}")
    assert after.status_code == 200
    assert after.json() == before_representation
    session = db.SessionLocal()
    try:
        record = session.get(db.TicketRecord, ticket_id)
        assert (record.tenant_key, record.title, record.status, record.priority, record.queue) == before_row
    finally:
        session.close()


def test_ticket_creation_requires_actor_tenant(fencing_client):
    client, set_actor = fencing_client
    set_actor(_tenant_actor("tenant-b"))
    response = client.post("/tickets", json={
        "id": 2002, "userId": 88, "title": "Cross-tenant write attempt",
        "completed": False, "tenant": "tenant-a",
    })
    assert response.status_code == 404, response.text
    stamped = client.post("/tickets", json={
        "id": 2003, "userId": 88, "title": "No tenant in payload",
        "completed": False,
    })
    assert stamped.status_code == 201, stamped.text
    set_actor(_local_actor())
    stored = {row["ticket_id"] for row in client.get("/tickets").json()}
    assert stamped.json()["ticket_id"] in stored
    session = db.SessionLocal()
    try:
        record = session.get(db.TicketRecord, stamped.json()["ticket_id"])
        assert record.tenant_key == "tenant-b"
    finally:
        session.close()


def test_local_actor_stays_global(fencing_client):
    client, set_actor = fencing_client
    set_actor(_tenant_actor("tenant-a"))
    first = client.post("/tickets", json={"id": 1, "userId": 1, "title": "One", "completed": False})
    set_actor(_tenant_actor("tenant-b"))
    second = client.post("/tickets", json={"id": 2, "userId": 2, "title": "Two", "completed": False})
    assert {first.status_code, second.status_code} == {201}
    set_actor(_local_actor())
    assert len(client.get("/tickets").json()) == 2
    set_actor(_tenant_actor("tenant-a"))
    assert len(client.get("/tickets").json()) == 1


def test_tenant_actor_cannot_read_unassigned_legacy_records(fencing_client):
    client, set_actor = fencing_client
    set_actor(_local_actor())
    ticket = client.post("/tickets", json={
        "id": 301, "userId": 3, "title": "Legacy unassigned ticket", "completed": False,
    })
    request = client.post("/onboarding-requests", json={**ONBOARDING, "employee_email": "legacy@example.com"})
    assert ticket.status_code == 201, ticket.text
    assert request.status_code == 201, request.text

    set_actor(_tenant_actor("tenant-a"))
    assert client.get(f"/tickets/{ticket.json()['ticket_id']}").status_code == 404
    assert client.get(f"/onboarding-requests/{request.json()['id']}").status_code == 404


def test_workflow_runs_and_jobs_are_fenced_by_request_tenant(fencing_client):
    client, set_actor = fencing_client
    set_actor(_tenant_actor("tenant-a"))
    request_a = client.post("/onboarding-requests", json=ONBOARDING)
    set_actor(_tenant_actor("tenant-b"))
    request_b = client.post("/onboarding-requests", json={**ONBOARDING, "employee_email": "b@example.com"})
    assert request_a.status_code == request_b.status_code == 201

    session = db.SessionLocal()
    try:
        run_a = db.WorkflowRunRecord(request_id=request_a.json()["id"], status="running")
        run_b = db.WorkflowRunRecord(request_id=request_b.json()["id"], status="running")
        session.add_all([run_a, run_b])
        session.flush()
        job_a = db.WorkflowJobRecord(
            job_type="onboarding.item.execute", payload_json="{}", status="failed",
            resource_type="onboarding", resource_id=str(request_a.json()["id"]),
        )
        job_b = db.WorkflowJobRecord(
            job_type="onboarding.item.execute", payload_json="{}", status="failed",
            resource_type="onboarding", resource_id=str(request_b.json()["id"]),
        )
        session.add_all([job_a, job_b])
        session.commit()
        run_a_id, run_b_id, job_a_id, job_b_id = run_a.id, run_b.id, job_a.id, job_b.id
    finally:
        session.close()

    set_actor(_tenant_actor("tenant-a"))
    assert client.get("/workflow-runs").json()[0]["id"] == run_a_id
    assert client.get("/workflow-jobs").json()[0]["id"] == job_a_id
    set_actor(_tenant_actor("tenant-b"))
    assert client.get(f"/onboarding-requests/{request_a.json()['id']}/run").status_code == 404
    assert client.post(f"/workflow-runs/{run_a_id}/cancel").status_code == 404
    assert client.get(f"/workflow-jobs/{job_a_id}").status_code == 404
    assert client.get(f"/workflow-jobs/{job_a_id}/history").status_code == 404
    assert client.post(f"/workflow-jobs/{job_a_id}/retry").status_code == 404


def test_guessed_foreign_workflow_run_actions_are_fenced(fencing_client):
    client, set_actor = fencing_client
    set_actor(_tenant_actor("tenant-a"))
    request = client.post("/onboarding-requests", json=ONBOARDING)
    assert request.status_code == 201, request.text
    session = db.SessionLocal()
    try:
        run = db.WorkflowRunRecord(request_id=request.json()["id"], status="running")
        session.add(run)
        session.commit()
        run_id = run.id
    finally:
        session.close()

    set_actor(_tenant_actor("tenant-b"))
    for action in ("pause", "resume", "cancel"):
        response = client.post(f"/workflow-runs/{run_id}/{action}")
        assert response.status_code == 404, response.text

    session = db.SessionLocal()
    try:
        assert session.get(db.WorkflowRunRecord, run_id).status == "running"
    finally:
        session.close()


def test_operator_routes_do_not_accept_missing_or_foreign_record_ids(fencing_client):
    client, set_actor = fencing_client
    set_actor(_tenant_actor("tenant-a"))
    request = client.post("/onboarding-requests", json=ONBOARDING)
    assert request.status_code == 201, request.text
    request_id = request.json()["id"]
    session = db.SessionLocal()
    try:
        run = db.WorkflowRunRecord(request_id=request_id, status="running")
        session.add(run)
        session.flush()
        job = db.WorkflowJobRecord(
            job_type="onboarding.item.execute", payload_json="{}", status="failed",
            resource_type="onboarding", resource_id=str(request_id),
        )
        session.add(job)
        session.commit()
        run_id, job_id = run.id, job.id
    finally:
        session.close()

    set_actor(_tenant_actor("tenant-b"))
    expected_status = {
        f"/workflow-runs/{run_id}/cancel": 404,
        f"/workflow-jobs/{job_id}/retry": 404,
        f"/workflow-jobs/{job_id}/cancel": 403,  # admin-only operator route
        "/workflow-runs/999999/pause": 404,
        "/workflow-jobs/999999/retry": 404,
    }
    for url, expected in expected_status.items():
        response = client.post(url)
        assert response.status_code == expected, f"{url}: {response.status_code} {response.text}"


def test_psa_sync_rejects_unassigned_and_foreign_tickets(fencing_client, monkeypatch):
    client, set_actor = fencing_client
    set_actor(_local_actor())
    unassigned = client.post("/tickets", json={
        "id": 711, "userId": 7, "title": "Legacy ticket", "completed": False,
    })
    set_actor(_tenant_actor("tenant-b"))
    foreign = client.post("/tickets", json={
        "id": 712, "userId": 7, "title": "Tenant B ticket", "completed": False,
    })
    assert unassigned.status_code == foreign.status_code == 201

    sync_calls = []
    monkeypatch.setattr(connectwise, "client_from_environment", lambda: object())
    monkeypatch.setattr(
        connectwise, "sync_ticket",
        lambda *_args: sync_calls.append(_args[1].ticket_id) or {"action": "created", "connectwise_id": "1"},
    )
    set_actor(_tenant_actor("tenant-a"))
    for ticket_id in (unassigned.json()["ticket_id"], foreign.json()["ticket_id"]):
        response = client.post(f"/tickets/{ticket_id}/psa-sync")
        assert response.status_code == 404, response.text
    assert sync_calls == []


def test_onboarding_requests_are_tenant_scoped(fencing_client):
    client, set_actor = fencing_client
    set_actor(_tenant_actor("tenant-a"))
    created = client.post("/onboarding-requests", json=ONBOARDING)
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]
    assert created.json()["tenant_key"] == "tenant-a"

    set_actor(_tenant_actor("tenant-b"))
    assert client.get("/onboarding-requests").json() == []
    assert client.get(f"/onboarding-requests/{request_id}").status_code == 404
    assert client.get(f"/onboarding-requests/{request_id}/status").status_code == 404
    foreign = client.post("/onboarding-requests", json={**ONBOARDING, "tenant_key": "tenant-a"})
    assert foreign.status_code == 404, foreign.text

    set_actor(_tenant_actor("tenant-a"))
    assert client.get(f"/onboarding-requests/{request_id}").status_code == 200


def test_chatops_token_cannot_leave_its_tenant(fencing_client, monkeypatch):
    client, set_actor = fencing_client
    secret = "chatops-test-secret"
    monkeypatch.setenv("FLOWOPS_CHATOPS_SECRET", secret)
    environ = {"FLOWOPS_CHATOPS_SECRET": secret}
    set_actor(_tenant_actor("tenant-a"))
    created = client.post("/onboarding-requests", json=ONBOARDING)
    assert created.status_code == 201, created.text
    record_id = created.json()["id"]
    item = next(item for item in created.json()["checklist"] if item["requires_approval"])
    token = chatops.generate_token(
        record_id, item["id"], "approve", tenant_key="tenant-b", environ=environ,
    )

    # The action itself is actor-free; the token's tenant binding guards it.
    assert client.post(f"/webhooks/actions/approve?token={token}").status_code == 404
    token = chatops.generate_token(
        record_id, item["id"], "approve", tenant_key="tenant-a", environ=environ,
    )
    approval = client.post(f"/webhooks/actions/approve?token={token}")
    assert approval.status_code == 200, approval.text
    replay = client.post(f"/webhooks/actions/approve?token={token}")
    assert replay.status_code == 409, replay.text


def test_chatops_token_without_tenant_still_actions(fencing_client, monkeypatch):
    """Legacy tokens minted before tenant binding keep working."""
    client, set_actor = fencing_client
    secret = "chatops-test-secret"
    monkeypatch.setenv("FLOWOPS_CHATOPS_SECRET", secret)
    environ = {"FLOWOPS_CHATOPS_SECRET": secret}
    set_actor(_tenant_actor("tenant-a"))
    created = client.post("/onboarding-requests", json=ONBOARDING)
    assert created.status_code == 201, created.text
    record_id = created.json()["id"]
    item = next(item for item in created.json()["checklist"] if item["requires_approval"])
    token = chatops.generate_token(record_id, item["id"], "approve", environ=environ)
    response = client.post(f"/webhooks/actions/approve?token={token}")
    assert response.status_code == 200, response.text


def test_trigger_rules_are_tenant_fenced(fencing_client):
    client, set_actor = fencing_client
    admin_tenant = lambda key: Actor(f"admin-{key}", ("automation.operator", "automation.admin"), source="bearer", tenant_key=key)
    set_actor(admin_tenant("tenant-a"))
    rule_payload = {
        "name": "auto-escalate",
        "tenant_key": "tenant-a",
        "source": "webhook",
        "event_type": "ticket.created",
        "predicate": "event.priority == \"urgent\"",
        "action_type": "start_workflow",
        "request_type": "onboarding",
        "static_inputs": {},
        "enabled": True,
    }
    created = client.post("/trigger-rules", json=rule_payload)
    assert created.status_code == 201, created.text
    rule_id = created.json()["id"]
    assert created.json()["tenant_key"] == "tenant-a"

    # Cross-tenant actor cannot see, update or delete it
    set_actor(admin_tenant("tenant-b"))
    assert client.get("/trigger-rules").json() == []
    assert client.put(f"/trigger-rules/{rule_id}", json=rule_payload).status_code == 404
    assert client.delete(f"/trigger-rules/{rule_id}").status_code == 404

    # Foreign tenant payload creation rejected
    foreign = client.post("/trigger-rules", json={**rule_payload, "tenant_key": "tenant-a"})
    assert foreign.status_code == 404, foreign.text

    # Own tenant can see and delete it
    set_actor(admin_tenant("tenant-a"))
    rules = client.get("/trigger-rules").json()
    assert len(rules) == 1 and rules[0]["id"] == rule_id
    # Omitting the optional tenant in a full replacement must preserve the
    # authenticated tenant instead of turning the rule into a global rule.
    unscoped_update = {
        key: value for key, value in rule_payload.items() if key != "tenant_key"
    }
    updated = client.put(f"/trigger-rules/{rule_id}", json=unscoped_update)
    assert updated.status_code == 200, updated.text
    assert updated.json()["tenant_key"] == "tenant-a"
    assert client.delete(f"/trigger-rules/{rule_id}").status_code == 200


def test_connectors_are_authenticated_tenant_scoped_and_stamp_webhook_events(
    fencing_client,
):
    client, set_actor = fencing_client
    admin = lambda key: Actor(
        f"admin-{key}",
        ("automation.operator", "automation.admin"),
        source="bearer",
        tenant_key=key,
    )
    set_actor(_tenant_actor("tenant-a"))
    assert client.get("/connectors").status_code == 403

    set_actor(admin("tenant-a"))
    payload = {
        "name": "Tenant A webhook",
        "connector_type": "generic",
        "config": {},
        "tenant_key": "tenant-a",
    }
    created = client.post("/connectors", json=payload)
    assert created.status_code == 201, created.text
    connector_id = created.json()["id"]

    delivered = client.post(
        f"/connectors/{connector_id}/webhook",
        json={"type": "alert", "id": "tenant-event", "tenant": "tenant-b"},
    )
    assert delivered.status_code == 201, delivered.text
    session = db.SessionLocal()
    try:
        event = session.query(db.EventRecord).filter_by(external_id="tenant-event").one()
        assert event.tenant_key == "tenant-a"
    finally:
        session.close()

    set_actor(admin("tenant-b"))
    assert client.get("/connectors").json() == []
    assert client.put(f"/connectors/{connector_id}", json=payload).status_code == 404
    assert client.delete(f"/connectors/{connector_id}").status_code == 404
    cross_tenant = client.post("/connectors", json=payload)
    assert cross_tenant.status_code == 404

    set_actor(admin("tenant-a"))
    unscoped_update = {key: value for key, value in payload.items() if key != "tenant_key"}
    updated = client.put(f"/connectors/{connector_id}", json=unscoped_update)
    assert updated.status_code == 200, updated.text
    assert updated.json()["tenant_key"] == "tenant-a"
    assert client.delete(f"/connectors/{connector_id}").status_code == 200


def test_tenant_connector_polling_passes_actor_scope(fencing_client, monkeypatch):
    from src import connectors

    client, set_actor = fencing_client
    set_actor(
        Actor(
            "admin-tenant-a",
            ("automation.operator", "automation.admin"),
            source="bearer",
            tenant_key="tenant-a",
        )
    )
    tenants = []
    monkeypatch.setattr(
        connectors,
        "poll_all_connectors",
        lambda **kwargs: tenants.append(kwargs.get("tenant_key")) or 2,
    )
    response = client.post("/connectors/poll")
    assert response.status_code == 200
    assert response.json() == {"ingested": 2}
    assert tenants == ["tenant-a"]
