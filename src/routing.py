"""Rule-based ticket classification, prioritization, and routing (v0.4).

Pure functions only — no I/O — so routing decisions are cheap to test and
easy to reason about (the same shape as branch logic in Rewst/n8n).
"""

from dataclasses import dataclass

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "password": ("password", "passcode", "locked out", "sign in", "log in", "mfa"),
    "email": ("email", "outlook", "mailbox", "distribution list"),
    "network": ("vpn", "wi-fi", "wifi", "network", "internet", "connectivity"),
    "hardware": (
        "laptop",
        "monitor",
        "keyboard",
        "mouse",
        "printer",
        "docking station",
        "screen",
        "headset",
    ),
    "software": ("software", "install", "license", "application", "update", "teams", "excel"),
    "access": ("access", "permission", "sharepoint", "shared drive", "folder", "group"),
}
DEFAULT_CATEGORY = "general"

URGENT_KEYWORDS = ("outage", "all users", "everyone", "cannot work", "is down", "system down")
HIGH_KEYWORDS = ("urgent", "asap", "blocked", "security", "phishing", "breach")

QUEUE_BY_CATEGORY = {
    "password": "it-access-management",
    "access": "it-access-management",
    "email": "it-messaging",
    "network": "it-network",
    "hardware": "it-hardware",
    "software": "it-software",
    "general": "it-service-desk",
}


@dataclass(frozen=True)
class RoutingDecision:
    category: str
    priority: str
    queue: str


def classify(title: str) -> str:
    """Map a ticket title to a category using keyword rules."""
    text = title.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return category
    return DEFAULT_CATEGORY


def assign_priority(title: str, category: str) -> str:
    """Assign urgent/high/normal from keywords and category."""
    text = title.lower()
    if any(keyword in text for keyword in URGENT_KEYWORDS):
        return "urgent"
    if any(keyword in text for keyword in HIGH_KEYWORDS):
        return "high"
    if category == "password":
        return "high"
    return "normal"


def queue_for(category: str) -> str:
    """Map a category to the support queue that owns it."""
    return QUEUE_BY_CATEGORY.get(category, QUEUE_BY_CATEGORY[DEFAULT_CATEGORY])


def route_ticket(title: str) -> RoutingDecision:
    """Full routing decision for a ticket title."""
    category = classify(title)
    return RoutingDecision(
        category=category,
        priority=assign_priority(title, category),
        queue=queue_for(category),
    )
