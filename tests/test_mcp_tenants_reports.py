"""MCP tools, tenant API, cross-tenant drift and SQL reports (in-process)."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from src import api, db
from src.mcp_server import TOOL_NAMES, FlowOpsToolError, FlowOpsTools, build_server

TICKET = {"userId": 1, "id": 101, "title": "VPN not connecting", "completed": False, "tenant": "acme"}
ONBOARD = {
    "request_type": "onboarding", "employee_name": "Jane Doe", "employee_email": "jane@acme.com",
    "department": "Sales", "requester_id": 7, "tenant_key": "acme",
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWOPS_ACTION_ADAPTER", "simulated")
    db.use_database(f"sqlite:///{tmp_path / 'mcp.db'}")
    with TestClient(api.app) as test_client:
        yield test_client


def _seed_tenants(client):
    for key, profile in (("acme", "healthy"), ("globex", "licence_exhausted")):
        response = client.put(f"/tenants/{key}", json={
            "name": key.title(), "usage_location": "GB", "licence_sku": "O365_BUSINESS_PREMIUM",
            "psa_company_id": "250", "baseline": {"simulated_profile": profile, "max_unlicensed_users": 1},
        })
        assert response.status_code == 200, response.text


def test_tenant_crud_validation_and_tenant_scoped_records(client):
    _seed_tenants(client)
    assert [t["key"] for t in client.get("/tenants").json()] == ["acme", "globex"]
    assert client.put("/tenants/Bad Key", json={"name": "x"}).status_code == 422
    assert client.put("/tenants/x", json={"name": "x", "credential_ref": "has space"}).status_code == 422

    assert client.post("/tickets", json=TICKET).status_code == 201
    created = client.post("/onboarding-requests", json=ONBOARD)
    assert created.status_code == 201 and created.json()["tenant_key"] == "acme"
    unknown = client.post("/onboarding-requests", json={**ONBOARD, "tenant_key": "nope"})
    assert unknown.status_code == 422
    with db.SessionLocal() as session:
        assert session.get(db.TicketRecord, "101").tenant_key == "acme"


def test_drift_check_endpoint_runs_across_tenants_and_logs(client):
    _seed_tenants(client)
    body = client.post("/tenants/drift-check").json()
    assert body["checked"] == 2 and body["drifted"] == 1 and body["errors"] == 0
    statuses = {r["tenant"]: r["status"] for r in body["results"]}
    assert statuses == {"acme": "compliant", "globex": "drift"}
    with db.SessionLocal() as session:
        actions = [r.action for r in session.query(db.ExecutionLogRecord).all()]
    assert actions.count("tenant.drift_check") == 2
    assert client.post("/tenants/drift-check?tenant=missing").status_code == 404


def test_reports_views_return_kpis(client):
    _seed_tenants(client)
    client.post("/tickets", json=TICKET)
    client.post("/tickets", json={**TICKET, "id": 102, "tenant": "globex", "title": "Printer jam"})
    summary = client.get("/reports/summary?days=7").json()
    assert {"automation_daily", "action_reliability", "ticket_backlog", "time_saved", "tenant_kpis"} <= set(summary)
    backlog = {(r["tenant_key"], r["queue"]): r["open_tickets"] for r in summary["ticket_backlog"]}
    assert sum(backlog.values()) == 2
    assert {r["tenant_key"] for r in summary["tenant_kpis"]} == {"acme", "globex"}
    assert any(r["action"] == "ticket.created" and r["runs"] == 2 for r in summary["automation_daily"])
    assert client.get("/reports/nope").status_code == 404
    assert client.get("/reports/ticket-backlog").status_code == 200


def test_mcp_tools_go_through_the_api(client):
    _seed_tenants(client)
    client.post("/tickets", json=TICKET)
    read_only = FlowOpsTools(client)
    assert read_only.list_tickets(status="open")[0]["ticket_id"] == "101"
    triage = read_only.triage_ticket("101")
    assert "summary" in triage and "recommendations" in triage
    assert read_only.tenant_drift_report("acme")["results"][0]["status"] == "compliant"
    assert "tenant_kpis" in read_only.automation_kpis(days=7)
    with pytest.raises(FlowOpsToolError, match="FLOWOPS_MCP_ALLOW_WRITES"):
        read_only.request_onboarding("onboarding", "A", "a@acme.com", "IT", 1)
    with pytest.raises(FlowOpsToolError, match="404"):
        read_only.get_ticket("999")

    writer = FlowOpsTools(client, allow_writes=True)
    first = writer.request_onboarding("onboarding", "Jane Doe", "jane@acme.com", "Sales", 7, tenant_key="acme")
    again = writer.request_onboarding("onboarding", "Jane Doe", "jane@acme.com", "Sales", 7, tenant_key="acme")
    assert first["id"] == again["id"]  # Idempotency-Key derived from the payload
    pending = read_only.list_pending_approvals()
    assert pending and all(p["tenant_key"] == "acme" for p in pending)
    assert isinstance(read_only.list_failed_jobs(), list)
    with pytest.raises(FlowOpsToolError, match="requires tenant_key"):
        writer.request_onboarding("onboarding", "A", "a@acme.com", "IT", 1)


def test_mcp_server_registers_and_calls_tools(client):
    server = build_server(FlowOpsTools(client))
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == set(TOOL_NAMES)
    client.post("/tickets", json=TICKET)
    result = asyncio.run(server.call_tool("get_ticket", {"ticket_id": "101"}))
    assert "VPN not connecting" in str(result)


def test_mcp_workflow_and_trigger_reads_enforce_backend_scope(client, monkeypatch):
    _seed_tenants(client)
    key = "mcp-definition"
    definition = client.post("/workflow-definitions", json={
        "key": key, "name": "MCP definition", "request_type": "onboarding",
        "steps": [{"key": "manual", "step": "Manual step", "queue": "it-service-desk"}],
    })
    assert definition.status_code == 200, definition.text
    assert client.post(f"/workflow-definitions/{key}/publish", json={"version": 1}).status_code == 200
    with db.SessionLocal() as session:
        session.add(db.EventSourceRecord(
            key="mcp-source", name="MCP source", secret="synthetic-test-secret", global_scope=True,
            secret_generated=True, active=True,
        ))
        session.add(db.TriggerRecord(
            key="mcp-trigger", name="MCP trigger", source_key="mcp-source", event_type="user.created",
            target_type="onboarding_request", definition_key=key, mapping_json="{}", schedule_kind="webhook", active=True,
        ))
        session.commit()

    run_ids = {}
    for tenant in ("acme", "globex"):
        request = client.post("/onboarding-requests", json={**ONBOARD, "tenant_key": tenant, "definition_key": key})
        assert request.status_code == 201, request.text
        started = client.post(f"/onboarding-requests/{request.json()['id']}/run")
        assert started.status_code == 200, started.text
        run_ids[tenant] = started.json()["id"]

    reader = FlowOpsTools(client)
    assert reader.get_workflow_definition(key)["current_version"] == 1
    assert key in {row["key"] for row in reader.list_workflow_definitions()}
    assert reader.get_trigger("mcp-trigger")["definition_key"] == key
    assert "mcp-trigger" in {row["key"] for row in reader.list_triggers()}
    assert [row["id"] for row in reader.list_workflow_runs(tenant="acme", definition=key)] == [run_ids["acme"]]
    assert reader.list_workflow_runs(tenant="acme", offset=1) == []
    assert reader.get_workflow_run(run_ids["globex"])["id"] == run_ids["globex"]
    with pytest.raises(FlowOpsToolError, match="Invalid resource key"):
        reader.get_workflow_definition("../workflow-runs")
    with pytest.raises(FlowOpsToolError, match="Invalid resource key"):
        reader.get_trigger("mcp-trigger%2fdeliveries")

    monkeypatch.setenv("FLOWOPS_AUTH_REQUIRED", "true")
    monkeypatch.setenv("FLOWOPS_ENVIRONMENT", "development")
    monkeypatch.setenv("FLOWOPS_DEV_BEARER_TOKEN", "mcp-test-token")
    monkeypatch.setenv("FLOWOPS_DEV_TENANT_KEY", "acme")
    with pytest.raises(FlowOpsToolError, match="401"):
        reader.list_workflow_runs()
    client.headers["Authorization"] = "Bearer mcp-test-token"
    assert [row["id"] for row in reader.list_workflow_runs()] == [run_ids["acme"]]
    assert reader.get_workflow_run(run_ids["acme"])["id"] == run_ids["acme"]
    with pytest.raises(FlowOpsToolError, match="404"):
        reader.get_workflow_run(run_ids["globex"])
    for admin_read in (reader.list_workflow_definitions, reader.list_triggers,
                       lambda: reader.get_workflow_definition(key), lambda: reader.get_trigger("mcp-trigger")):
        with pytest.raises(FlowOpsToolError, match="403"):
            admin_read()
