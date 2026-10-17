"""Employee onboarding/offboarding checklist templates (v0.5).

Pure data and helpers; persistence lives in src/db.py and the API in
src/api.py. Later (v0.6) each checklist step gains a real action behind
it via Power Automate / Microsoft Graph.
"""

from typing import TypedDict

VALID_REQUEST_TYPES = ("onboarding", "offboarding")


class ChecklistStep(TypedDict):
    step: str
    key: str  # stable identifier used by the offboarding completion ledger
    queue: str
    requires_approval: bool
    action: str  # which automation runs when this step is completed


ONBOARDING_STEPS: tuple[ChecklistStep, ...] = (
    {"step": "Create user account", "key": "create_user_account", "queue": "it-access-management", "requires_approval": True, "action": "none"},
    {"step": "Provision laptop and peripherals", "key": "provision_laptop", "queue": "it-hardware", "requires_approval": False, "action": "none"},
    {"step": "Grant software licenses", "key": "grant_licenses", "queue": "it-software", "requires_approval": True, "action": "none"},
    {"step": "Configure email mailbox", "key": "configure_mailbox", "queue": "it-messaging", "requires_approval": False, "action": "none"},
)

OFFBOARDING_STEPS: tuple[ChecklistStep, ...] = (
    {"step": "Disable user account", "key": "disable_user_account", "queue": "it-access-management", "requires_approval": True, "action": "none"},
    {"step": "Recover laptop and peripherals", "key": "recover_laptop", "queue": "it-hardware", "requires_approval": False, "action": "none"},
    {"step": "Revoke software licenses", "key": "revoke_licenses", "queue": "it-software", "requires_approval": False, "action": "none"},
    {"step": "Archive email mailbox", "key": "archive_mailbox", "queue": "it-messaging", "requires_approval": True, "action": "none"},
)

# v0.6 placeholder: an extra onboarding step that provisions a *simulated*
# Microsoft 365 account (src/m365_simulator.py). Added only when the request
# carries a mail address; no real credentials are used anywhere.
M365_PROVISION_STEP: ChecklistStep = {
    "step": "Provision Microsoft 365 account (simulated)",
    "key": "m365_provision",
    "queue": "it-access-management",
    "requires_approval": False,
    "action": "m365_provision",
}


def checklist_for(request_type: str, include_m365: bool = False) -> list[ChecklistStep]:
    """Return the checklist template for a request type.

    ``include_m365`` adds the simulated M365 provisioning step, but only
    for onboarding — offboarding never creates accounts.
    """
    if request_type == "onboarding":
        steps = list(ONBOARDING_STEPS)
        if include_m365:
            steps.append(dict(M365_PROVISION_STEP))
        return steps
    if request_type == "offboarding":
        return list(OFFBOARDING_STEPS)
    raise ValueError(
        f"Unknown request type: {request_type!r}. Expected one of {VALID_REQUEST_TYPES}."
    )
