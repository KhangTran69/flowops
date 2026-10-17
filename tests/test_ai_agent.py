"""Offline tests for the v0.7 AI support agent, KB, and approval gate."""

import pytest
from fastapi.testclient import TestClient

from src import ai_agent, api, db

TICKET = {
    "userId": 7,
    "id": 42,
    "title": "VPN outage - all users cannot work",
    "completed": False,
}


@pytest.fixture
def client(tmp_path):
    db.use_database(f"sqlite:///{tmp_path}/ai.db")
    return TestClient(api.app)


def _create_misrouted_ticket(client, ticket_id=42):
    response = client.post("/tickets", json={**TICKET, "id": ticket_id})
    assert response.status_code == 201
    session = db.SessionLocal()
    try:
        record = session.get(db.TicketRecord, str(ticket_id))
        record.queue = "it-service-desk"
        record.priority = "normal"
        session.commit()
    finally:
        session.close()
    return str(ticket_id)


def _snapshot(title="VPN outage - all users cannot work", queue="it-service-desk", priority="normal"):
    return ai_agent.TicketSnapshot(
        ticket_id="42",
        requester_id=7,
        title=title,
        status="open",
        priority=priority,
        category="network",
        queue=queue,
    )


def test_knowledge_base_search_matches_vpn_password_and_hardware():
    vpn = ai_agent.search_knowledge_base("VPN cannot connect remotely")
    assert vpn
    assert vpn[0].article.id == "kb-vpn"

    password = ai_agent.search_knowledge_base("locked out password reset")
    assert any(match.article.id == "kb-password-reset" for match in password)

    hardware = ai_agent.search_knowledge_base("laptop monitor docking station")
    assert any(match.article.id == "kb-hardware" for match in hardware)


def test_simulated_agent_summarizes_and_recommends():
    result = ai_agent.SimulatedAIAdapter().analyze(
        _snapshot(),
        history=[{"action": "ticket.created", "status": "success"}],
    )
    assert result.adapter == "simulated"
    assert "VPN outage" in result.summary.text
    assert "outage" in result.summary.urgency_signals
    assert result.summary.history_count == 1
    ids = {recommendation.id for recommendation in result.recommendations}
    assert "queue-reassign" in ids
    assert "priority-upgrade" in ids
    assert result.knowledge_base


def test_triage_endpoint_does_not_mutate_and_logs_correlation(client):
    ticket_id = _create_misrouted_ticket(client)
    response = client.post(f"/tickets/{ticket_id}/ai-triage")
    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["requester_id"] == 7
    assert body["knowledge_base"]
    recommendations = {row["id"]: row for row in body["recommendations"]}
    assert recommendations["queue-reassign"]["suggested"] == "it-network"
    assert recommendations["priority-upgrade"]["suggested"] == "urgent"

    stored = client.get(f"/tickets/{ticket_id}").json()
    assert stored["queue"] == "it-service-desk"
    assert stored["priority"] == "normal"

    logs = client.get("/execution-logs", params={"actor": "local-development"}).json()
    actions = [row["action"] for row in logs]
    assert "ai.summarize" in actions
    assert "ai.recommend" in actions
    ai_logs = [row for row in logs if row["action"].startswith("ai.")]
    assert all(row["correlation_id"] for row in ai_logs)


def test_apply_requires_operator_auth_and_updates_only_queue_priority(client, monkeypatch):
    ticket_id = _create_misrouted_ticket(client)
    triage = client.post(f"/tickets/{ticket_id}/ai-triage").json()
    recommendation_ids = [
        row["id"] for row in triage["recommendations"] if row["applicable"]
    ]
    assert recommendation_ids

    monkeypatch.setenv("FLOWOPS_AUTH_REQUIRED", "true")
    monkeypatch.setenv("FLOWOPS_DEV_BEARER_TOKEN", "dev-secret")
    path = f"/tickets/{ticket_id}/ai-recommendations/apply"
    payload = {"recommendation_ids": recommendation_ids}
    assert client.post(path, json=payload).status_code == 401
    applied = client.post(
        path, json=payload, headers={"Authorization": "Bearer dev-secret"}
    )
    assert applied.status_code == 200
    assert set(applied.json()["applied"]) == set(recommendation_ids)
    ticket = applied.json()["ticket"]
    assert ticket["queue"] == "it-network"
    assert ticket["priority"] == "urgent"
    assert ticket["status"] == "open"  # AI never changes status

    logs = client.get("/audit").json()
    applied_logs = [row for row in logs if row["action"] == "ai.recommendation_applied"]
    assert applied_logs
    assert applied_logs[0]["actor_subject"] == "configured-bearer"
    assert applied_logs[0]["correlation_id"]


def test_apply_rejects_unknown_and_informational_recommendations(client):
    ticket_id = _create_misrouted_ticket(client)
    path = f"/tickets/{ticket_id}/ai-recommendations/apply"
    unknown = client.post(path, json={"recommendation_ids": ["does-not-exist"]})
    assert unknown.status_code == 404

    triage = client.post(f"/tickets/{ticket_id}/ai-triage").json()
    informational = [
        row["id"] for row in triage["recommendations"] if not row["applicable"]
    ]
    if informational:
        response = client.post(
            path, json={"recommendation_ids": [informational[0]]}
        )
        assert response.status_code == 409
    stored = client.get(f"/tickets/{ticket_id}").json()
    assert stored["queue"] == "it-service-desk"


def test_knowledge_base_endpoint_lists_and_searches(client):
    all_articles = client.get("/knowledge-base").json()
    assert len(all_articles) >= 6
    searched = client.get("/knowledge-base", params={"query": "vpn"}).json()
    assert searched and searched[0]["id"] == "kb-vpn"
    filtered = client.get("/knowledge-base", params={"category": "hardware"}).json()
    assert filtered and all(item["category"] == "hardware" for item in filtered)


def test_llm_adapter_boundary_uses_injected_client():
    calls = []

    def fake_client(prompt):
        calls.append(prompt)
        return (
            '{"summary": "LLM summary for the outage.",'
            ' "recommendations": [{"id": "llm-1", "kind": "priority",'
            ' "title": "Escalate", "current": "normal", "suggested": "urgent",'
            ' "rationale": "Outage", "confidence": 90, "applicable": true}]}'
        )

    adapter = ai_agent.LLMAIAdapter("test-key", client=fake_client)
    result = adapter.analyze(_snapshot())
    assert result.adapter == "llm"
    assert result.summary.text == "LLM summary for the outage."
    assert result.recommendations[0].id == "llm-1"
    assert calls

    with pytest.raises(ai_agent.AIAvailabilityError):
        ai_agent.LLMAIAdapter("test-key").analyze(_snapshot())


def test_adapter_selection_is_credential_free_by_default():
    assert isinstance(ai_agent.adapter_from_environment({}), ai_agent.SimulatedAIAdapter)
    assert isinstance(
        ai_agent.adapter_from_environment({"FLOWOPS_AI_API_KEY": "key"}),
        ai_agent.SimulatedAIAdapter,
    )
    assert isinstance(
        ai_agent.adapter_from_environment(
            {"FLOWOPS_AI_ADAPTER": "llm", "FLOWOPS_AI_API_KEY": "key"}
        ),
        ai_agent.LLMAIAdapter,
    )


def test_ui_ticket_list_and_triage_panel(client):
    ticket_id = _create_misrouted_ticket(client)
    listing = client.get("/ui/tickets")
    assert listing.status_code == 200
    assert "VPN outage" in listing.text

    detail = client.get(f"/ui/tickets/{ticket_id}?triage=1")
    assert detail.status_code == 200
    assert "AI Triage" in detail.text
    assert "queue-reassign" in detail.text
    assert f"/tickets/{ticket_id}/ai-recommendations/apply" in detail.text
