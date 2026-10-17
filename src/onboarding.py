"""Employee onboarding/offboarding checklist templates (v0.5).

Pure data and helpers; persistence lives in src/db.py and the API in
src/api.py. Later (v0.6) each checklist step gains a real action behind
it via Power Automate / Microsoft Graph.
"""

from typing import TypedDict

VALID_REQUEST_TYPES = ("onboarding", "offboarding")


class ChecklistStep(TypedDict):
    step: str
    queue: str
    requires_approval: bool


ONBOARDING_STEPS: tuple[ChecklistStep, ...] = (
    {"step": "Create user account", "queue": "it-access-management", "requires_approval": True},
    {"step": "Provision laptop and peripherals", "queue": "it-hardware", "requires_approval": False},
    {"step": "Grant software licenses", "queue": "it-software", "requires_approval": True},
    {"step": "Configure email mailbox", "queue": "it-messaging", "requires_approval": False},
)

OFFBOARDING_STEPS: tuple[ChecklistStep, ...] = (
    {"step": "Disable user account", "queue": "it-access-management", "requires_approval": True},
    {"step": "Recover laptop and peripherals", "queue": "it-hardware", "requires_approval": False},
    {"step": "Revoke software licenses", "queue": "it-software", "requires_approval": False},
    {"step": "Archive email mailbox", "queue": "it-messaging", "requires_approval": True},
)


def checklist_for(request_type: str) -> list[ChecklistStep]:
    """Return the checklist template for a request type."""
    if request_type == "onboarding":
        return list(ONBOARDING_STEPS)
    if request_type == "offboarding":
        return list(OFFBOARDING_STEPS)
    raise ValueError(
        f"Unknown request type: {request_type!r}. Expected one of {VALID_REQUEST_TYPES}."
    )
