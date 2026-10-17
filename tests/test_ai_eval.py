"""Deterministic offline quality evaluation for the simulated AI agent."""

import pytest

from src import ai_agent

CASES = [
    {
        "title": "VPN outage - all users cannot work",
        "expected_kb": {"kb-vpn", "kb-network-outage"},
        "expected_queue": "it-network",
        "expected_priority": "urgent",
    },
    {
        "title": "VPN not connecting from home",
        "expected_kb": {"kb-vpn"},
        "expected_queue": "it-network",
        "expected_priority": None,
    },
    {
        "title": "Locked out - cannot reset password",
        "expected_kb": {"kb-password-reset", "kb-mfa"},
        "expected_queue": "it-access-management",
        "expected_priority": "high",
    },
    {
        "title": "MFA authenticator replaced",
        "expected_kb": {"kb-mfa", "kb-password-reset"},
        "expected_queue": "it-access-management",
        "expected_priority": "high",
    },
    {
        "title": "Laptop screen flickers",
        "expected_kb": {"kb-hardware"},
        "expected_queue": "it-hardware",
        "expected_priority": None,
    },
    {
        "title": "Monitor won't turn on",
        "expected_kb": {"kb-hardware"},
        "expected_queue": "it-hardware",
        "expected_priority": None,
    },
    {
        "title": "Need access to SharePoint for new project",
        "expected_kb": {"kb-saas-access"},
        "expected_queue": "it-access-management",
        "expected_priority": None,
    },
    {
        "title": "Outlook mailbox not receiving email",
        "expected_kb": {"kb-email"},
        "expected_queue": "it-messaging",
        "expected_priority": None,
    },
    {
        "title": "Printer keeps jamming",
        "expected_kb": {"kb-hardware"},
        "expected_queue": "it-hardware",
        "expected_priority": None,
    },
    {
        "title": "New hire onboarding account",
        "expected_kb": {"kb-onboarding"},
        "expected_queue": "it-service-desk",
        "expected_priority": None,
        "workflow": True,
    },
    {
        "title": "Employee leaver offboarding access",
        "expected_kb": {"kb-onboarding", "kb-saas-access"},
        "expected_queue": "it-access-management",
        "expected_priority": None,
        "workflow": True,
    },
    {
        "title": "Phishing email reported",
        "expected_kb": {"kb-email"},
        "expected_queue": "it-messaging",
        "expected_priority": "high",
    },
]


def _snapshot(title, queue="it-service-desk", priority="normal"):
    return ai_agent.TicketSnapshot(
        ticket_id="eval",
        requester_id=7,
        title=title,
        status="open",
        priority=priority,
        category=None,
        queue=queue,
    )


def test_simulated_agent_meets_golden_kb_and_recommendation_thresholds():
    adapter = ai_agent.SimulatedAIAdapter()
    top1_hits = 0
    for case in CASES:
        result = adapter.analyze(_snapshot(case["title"]))
        assert result.knowledge_base, case["title"]
        top_match = result.knowledge_base[0].article.id
        if top_match in case["expected_kb"]:
            top1_hits += 1

        recommendations = {row.id: row for row in result.recommendations}
        if case["expected_queue"] != "it-service-desk":
            queue = recommendations.get("queue-reassign")
            assert queue is not None, case["title"]
            assert queue.suggested == case["expected_queue"], case["title"]
        if case["expected_priority"]:
            priority = recommendations.get("priority-upgrade")
            assert priority is not None, case["title"]
            assert priority.suggested == case["expected_priority"], case["title"]
        if case.get("workflow"):
            workflow = recommendations.get("onboarding-checklist")
            assert workflow is not None, case["title"]
            assert workflow.applicable is False

        for recommendation in result.recommendations:
            if recommendation.kind in ("response", "workflow"):
                assert recommendation.applicable is False, case["title"]

    accuracy = top1_hits / len(CASES)
    assert accuracy >= 0.75, f"top-1 KB accuracy dropped to {accuracy:.0%}"


def test_already_correctly_routed_ticket_has_no_actionable_recommendations():
    result = ai_agent.SimulatedAIAdapter().analyze(
        _snapshot("Printer jam", queue="it-hardware", priority="normal")
    )
    assert result.knowledge_base[0].article.id == "kb-hardware"
    assert not any(
        recommendation.applicable for recommendation in result.recommendations
    )


def test_ambiguous_ticket_does_not_invent_a_kb_match():
    result = ai_agent.SimulatedAIAdapter().analyze(
        _snapshot("Something is wrong with my computer")
    )
    assert result.knowledge_base == ()
    assert all(not recommendation.applicable for recommendation in result.recommendations)
