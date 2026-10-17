"""Tests for the rule-based routing engine (pure functions)."""

import pytest

from src.routing import assign_priority, classify, queue_for, route_ticket


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Laptop will not start", "hardware"),
        ("Cannot sign in — password locked out", "password"),
        ("VPN keeps dropping", "network"),
        ("Need access to the shared drive folder", "access"),
        ("Please install an Excel license", "software"),
        ("Outlook mailbox is full", "email"),
        ("Where can I find the policy document?", "general"),
    ],
)
def test_classify(title, expected):
    assert classify(title) == expected


def test_classify_is_case_insensitive():
    assert classify("NEW LAPTOP NEEDED") == "hardware"


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Email server is down for all users", "urgent"),
        ("Wi-Fi outage — nobody can work", "urgent"),
        ("Possible phishing email received", "high"),
        ("Cannot reset password", "high"),  # password category defaults to high
        ("Laptop will not start", "normal"),
    ],
)
def test_assign_priority(title, expected):
    assert assign_priority(title, classify(title)) == expected


@pytest.mark.parametrize(
    "category, expected",
    [
        ("hardware", "it-hardware"),
        ("password", "it-access-management"),
        ("access", "it-access-management"),
        ("general", "it-service-desk"),
        ("something-new", "it-service-desk"),  # unknown falls back to service desk
    ],
)
def test_queue_for(category, expected):
    assert queue_for(category) == expected


def test_route_ticket_combines_decision():
    decision = route_ticket("Printer is on fire, this is urgent")
    assert decision.category == "hardware"
    assert decision.priority == "high"
    assert decision.queue == "it-hardware"
