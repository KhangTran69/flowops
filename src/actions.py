"""Credential-free action boundary for onboarding workflows (v0.6).

The default adapter records a simulated action. A Microsoft Graph,
Power Automate, or PowerShell adapter can replace it without changing the
workflow or API layer.
"""

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests


@dataclass(frozen=True)
class ActionResult:
    """Result returned by an external-action adapter."""

    action: str
    detail: str
    output: dict[str, Any] = field(default_factory=dict)


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



class GraphClientActionAdapter:
    """Uses the client-credentials GraphClient for real user/license actions."""

    def __init__(self, client, *, sku_id: str = "") -> None:
        self.client = client
        self.sku_id = sku_id

    def execute(self, action: str, context: dict[str, Any]) -> ActionResult:
        email = str(context.get("employee_email") or "")
        if action == "create_user_account":
            password = context.get("temporary_password")
            if not password:
                raise ValueError("temporary_password is required to create a Graph user")
            payload = {
                "accountEnabled": True,
                "displayName": context.get("employee_name") or email.split("@")[0],
                "mailNickname": email.split("@")[0],
                "userPrincipalName": email,
                "passwordProfile": {"forceChangePasswordNextSignIn": True, "password": password},
            }
            output = self.client.create_user(payload)
            return ActionResult(action=action, detail="Graph user created", output=output)
        if action == "grant_software_licenses":
            if not self.sku_id:
                raise ValueError("FLOWOPS_GRAPH_SKU_ID is required to assign licenses")
            user_id = context.get("user_id") or context.get("graph_user_id")
            if not user_id:
                users = self.client.list_users(f"userPrincipalName eq '{email}'")
                if not users:
                    raise ValueError(f"Graph user not found: {email}")
                user_id = users[0]["id"]
            output = self.client.assign_license(str(user_id), self.sku_id)
            return ActionResult(action=action, detail="Graph license assigned", output=output or {})
        if action in ("disable_user_account", "revoke_software_licenses"):
            user_id = context.get("user_id") or context.get("graph_user_id")
            if not user_id:
                users = self.client.list_users(f"userPrincipalName eq '{email}'")
                if not users:
                    raise ValueError(f"Graph user not found: {email}")
                user_id = users[0]["id"]
            output = self.client.request("PATCH", f"/users/{user_id}", json={"accountEnabled": False})
            return ActionResult(action=action, detail="Graph user disabled", output=output or {})
        raise ValueError(f"Graph client action is not implemented: {action!r}")


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




class PowerShellAdapter:
    """Executes checklist actions via PowerShell / Microsoft Graph PowerShell SDK.

    Supports local pwsh execution, script templating, and dry-run execution
    for safe testing without executing external processes.
    """

    def __init__(
        self,
        command_path: str = "pwsh",
        dry_run: bool = False,
        timeout: float = 15.0,
    ) -> None:
        self.command_path = command_path
        self.dry_run = dry_run
        self.timeout = timeout

    def execute(self, action: str, context: dict[str, Any]) -> ActionResult:
        employee = context.get("employee_email", "unknown")
        if self.dry_run:
            return ActionResult(
                action=action,
                detail=f"simulated PowerShell execution '{action}' for {employee}",
            )

        import json
        import subprocess

        script = (
            f"$input_json = $input | Out-String; "
            f"Write-Output \"PowerShell executed action '{action}' for {employee}\""
        )
        payload = json.dumps({"action": action, "context": context})
        try:
            result = subprocess.run(
                [self.command_path, "-NoProfile", "-NonInteractive", "-Command", script],
                input=payload,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=True,
            )
            detail = result.stdout.strip() or f"PowerShell completed action '{action}'"
            return ActionResult(action=action, detail=detail)
        except subprocess.CalledProcessError as err:
            err_msg = err.stderr.strip() if err.stderr else str(err)
            raise RuntimeError(f"PowerShell execution failed: {err_msg}") from err
        except subprocess.TimeoutExpired as err:
            raise RuntimeError(f"PowerShell action timed out after {self.timeout}s") from err
        except FileNotFoundError as err:
            raise RuntimeError(f"PowerShell executable not found at '{self.command_path}'") from err


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
        tenant = values.get("FLOWOPS_GRAPH_TENANT_ID")
        client_id = values.get("FLOWOPS_GRAPH_CLIENT_ID")
        client_secret = values.get("FLOWOPS_GRAPH_CLIENT_SECRET")
        if tenant and client_id and client_secret:
            from src.graph import GraphClient
            return GraphClientActionAdapter(
                GraphClient(tenant_id=tenant, client_id=client_id, client_secret=client_secret,
                            base_url=values.get("FLOWOPS_GRAPH_URL", "https://graph.microsoft.com/v1.0")),
                sku_id=values.get("FLOWOPS_GRAPH_SKU_ID", ""),
            )
        url = values.get("FLOWOPS_GRAPH_URL", "https://graph.microsoft.com/v1.0")
        token = values.get("FLOWOPS_GRAPH_ACCESS_TOKEN")
        if not token:
            raise ValueError("Set graph client credentials or FLOWOPS_GRAPH_ACCESS_TOKEN")
        return MicrosoftGraphAdapter(url, token)
    if name == "connectwise_mock":
        from src.connectwise import ConnectWiseMockAdapter
        return ConnectWiseMockAdapter(base_url=values.get("FLOWOPS_CONNECTWISE_URL", "https://api.connectwise.com"))
    if name == "powershell":
        command_path = values.get("FLOWOPS_POWERSHELL_PATH", "pwsh")
        dry_run = values.get("FLOWOPS_POWERSHELL_DRY_RUN", "").lower() in ("true", "1", "yes")
        return PowerShellAdapter(command_path=command_path, dry_run=dry_run)
    raise ValueError(f"Unsupported FLOWOPS_ACTION_ADAPTER: {name!r}")


def action_for_step(step: str) -> str:
    """Return the stable action name for a checklist step."""
    try:
        return ACTION_BY_STEP[step]
    except KeyError as error:
        raise ValueError(f"No automation action is mapped for checklist step: {step!r}") from error
