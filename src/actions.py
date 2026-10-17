"""Credential-free action boundary for onboarding workflows (v0.6).

The default adapter records a simulated action. A Microsoft Graph,
Power Automate, or PowerShell adapter can replace it without changing the
workflow or API layer.
"""

import os
from dataclasses import dataclass
from typing import Any, Protocol

import requests


@dataclass(frozen=True)
class ActionResult:
    """Result returned by an external-action adapter."""

    action: str
    detail: str


class ActionAdapter(Protocol):
    """Interface implemented by real or simulated action providers."""

    def execute(self, action: str, context: dict[str, Any]) -> ActionResult:
        """Execute one named action for a checklist item."""


ACTION_BY_STEP: dict[str, str] = {
    "Create user account": "create_user_account",
    "Disable user account": "disable_user_account",
    "Grant software licenses": "grant_software_licenses",
    "Revoke software licenses": "revoke_software_licenses",
    "Configure email mailbox": "configure_email_mailbox",
    "Archive email mailbox": "archive_email_mailbox",
    "Provision laptop and peripherals": "provision_hardware",
    "Recover laptop and peripherals": "recover_hardware",
}


class SimulatedMicrosoftAdapter:
    """Safe local adapter used until real Microsoft credentials are supplied."""

    def execute(self, action: str, context: dict[str, Any]) -> ActionResult:
        employee = context["employee_email"]
        return ActionResult(
            action=action,
            detail=f"simulated Microsoft action '{action}' for {employee}",
        )


class MicrosoftGraphAdapter:
    """Calls a Microsoft Graph action endpoint with a bearer access token."""

    def __init__(self, url: str, token: str, timeout: float = 10.0) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def execute(self, action: str, context: dict[str, Any]) -> ActionResult:
        headers = {"Authorization": f"Bearer {self.token}"}
        if context.get("idempotency_key"):
            headers["Idempotency-Key"] = str(context["idempotency_key"])
        response = requests.post(
            f"{self.url}/{action}",
            json=context,
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return ActionResult(action=action, detail="Microsoft Graph action accepted")
class PowerAutomateAdapter:
    """POSTs sanitized checklist context to a Power Automate trigger."""

    def __init__(self, url: str, timeout: float = 10.0) -> None:
        self.url = url
        self.timeout = timeout

    def execute(self, action: str, context: dict[str, Any]) -> ActionResult:
        payload: dict[str, Any] = {"action": action, "context": context}
        if context.get("idempotency_key"):
            payload["idempotency_key"] = context["idempotency_key"]
        response = requests.post(
            self.url,
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return ActionResult(action=action, detail="Power Automate webhook accepted action")




def adapter_from_environment(environ: dict[str, str] | None = None) -> ActionAdapter:
    """Build the configured adapter; simulation remains the safe default."""
    values = environ if environ is not None else os.environ
    name = values.get("FLOWOPS_ACTION_ADAPTER", "simulated").lower()
    if name == "simulated":
        return SimulatedMicrosoftAdapter()
    if name == "power_automate":
        url = values.get("FLOWOPS_POWER_AUTOMATE_URL")
        if not url:
            raise ValueError("FLOWOPS_POWER_AUTOMATE_URL is required for power_automate")
        return PowerAutomateAdapter(url)
    if name == "graph":
        url = values.get("FLOWOPS_GRAPH_URL", "https://graph.microsoft.com/v1.0")
        token = values.get("FLOWOPS_GRAPH_ACCESS_TOKEN")
        if not token:
            raise ValueError("FLOWOPS_GRAPH_ACCESS_TOKEN is required for graph")
        return MicrosoftGraphAdapter(url, token)
    raise ValueError(f"Unsupported FLOWOPS_ACTION_ADAPTER: {name!r}")


def action_for_step(step: str) -> str:
    """Return the stable action name for a checklist step."""
    try:
        return ACTION_BY_STEP[step]
    except KeyError as error:
        raise ValueError(f"No automation action is mapped for checklist step: {step!r}") from error
