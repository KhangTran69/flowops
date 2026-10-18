"""Action boundary for onboarding/offboarding workflows.

The default adapter records a simulated action. The Microsoft Graph adapter
performs the real user/licence/mailbox calls per tenant and drops back to
Exchange Online PowerShell for the steps Graph cannot do (shared-mailbox
conversion). Power Automate and PowerShell adapters can replace it without
changing the workflow or API layer.
"""

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import requests


class ActionError(RuntimeError):
    """Base error for an external action with explicit retry semantics."""

    retryable = True
    category = "action_error"


class RetryableActionError(ActionError):
    category = "retryable_action_error"


class PermanentActionError(ActionError):
    retryable = False
    category = "permanent_action_error"


class ConfigurationActionError(PermanentActionError):
    category = "configuration_error"


class AuthenticationActionError(PermanentActionError):
    category = "authentication_error"


class RateLimitActionError(RetryableActionError):
    category = "rate_limit"


def action_error_from_response(response: requests.Response, adapter: str) -> ActionError:
    """Classify an HTTP response without retaining the provider response body."""
    status = response.status_code
    if status in (401, 403):
        return AuthenticationActionError(f"{adapter} rejected the request (HTTP {status}).")
    if status == 429:
        return RateLimitActionError(f"{adapter} rate limited the request (HTTP 429).")
    if 400 <= status < 500:
        return PermanentActionError(f"{adapter} rejected the request (HTTP {status}).")
    return RetryableActionError(f"{adapter} request failed (HTTP {status}).")


def ensure_action_response(response: requests.Response, adapter: str) -> None:
    """Raise a classified error while supporting simple test doubles."""
    if hasattr(response, "ok"):
        if not response.ok:
            raise action_error_from_response(response, adapter)
        return
    response.raise_for_status()


@dataclass(frozen=True)
class ActionResult:
    """Result returned by an external-action adapter."""

    action: str
    detail: str
    output: dict[str, Any] = field(default_factory=dict)
    adapter: str = ""
    external_reference: str | None = None
    duration_ms: int = 0
    idempotency_key: str | None = None
    correlation_id: str | None = None

    def __getitem__(self, key: str) -> Any:
        """Expose legacy mapping-style access for webhook adapters."""
        if key == "action":
            return self.action
        if key == "detail":
            return self.detail
        if key == "output":
            return self.output
        if key == "external_reference":
            return self.external_reference
        raise KeyError(key)


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

    adapter_name = "SimulatedMicrosoftAdapter"

    def execute(self, action: str, context: dict[str, Any]) -> ActionResult:
        employee = context["employee_email"]
        return ActionResult(
            action=action,
            detail=f"simulated Microsoft action '{action}' for {employee}",
            adapter=self.adapter_name,
            idempotency_key=context.get("idempotency_key"),
            correlation_id=context.get("correlation_id"),
        )


class PowerAutomateAdapter:
    """POSTs sanitized checklist context to a Power Automate trigger."""

    adapter_name = "power_automate"

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
        ensure_action_response(response, "Power Automate")
        return ActionResult(
            action=action,
            detail="Power Automate webhook accepted action",
            adapter=self.adapter_name,
            idempotency_key=context.get("idempotency_key"),
            correlation_id=context.get("correlation_id"),
        )


class GenericWebhookAdapter:
    """POSTs sanitized checklist context to an external webhook endpoint.

    Works with Make, Rewst, Zapier, or any generic webhook consumer.
    If a ``secret`` is provided, the ``X-Webhook-Secret`` header is set by
    default and can be overridden via ``headers``.
    """

    adapter_name = "generic_webhook"

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
        secret: str | None = None,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.secret = secret
        self.headers = dict(headers or {})
        if secret and "X-Webhook-Secret" not in self.headers:
            self.headers["X-Webhook-Secret"] = secret

    def execute(self, action: str, context: dict[str, Any]) -> ActionResult:
        payload: dict[str, Any] = {"action": action, "context": context}
        if context.get("idempotency_key"):
            payload["idempotency_key"] = context["idempotency_key"]
        response = requests.post(
            self.url,
            json=payload,
            headers=self.headers or None,
            timeout=self.timeout,
        )
        ensure_action_response(response, "Webhook")
        return ActionResult(
            action=action,
            detail="Generic webhook accepted action",
            adapter=self.adapter_name,
            idempotency_key=context.get("idempotency_key"),
            correlation_id=context.get("correlation_id"),
        )


class PowerShellError(ActionError):
    """A failed PowerShell action; ``retryable`` is False for bad input/config."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.category = "powershell_error"


DEFAULT_POWERSHELL_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "powershell" / "Invoke-FlowOpsAction.ps1"
)


class PowerShellAdapter:
    """Runs ``Invoke-FlowOpsAction.ps1`` (Exchange Online) and parses its JSON.

    The request goes to the script on stdin, so no user data is ever
    interpolated into a command line. The script writes one JSON line;
    exit code 2 means invalid input/configuration (not retried), any other
    non-zero code is treated as transient.
    """

    def __init__(
        self,
        command_path: str = "pwsh",
        dry_run: bool = False,
        timeout: float = 120.0,
        script_path: str | None = None,
    ) -> None:
        self.command_path = command_path
        self.dry_run = dry_run
        self.timeout = timeout
        self.script_path = str(script_path or DEFAULT_POWERSHELL_SCRIPT)

    def command(self) -> list[str]:
        args = [self.command_path, "-NoProfile", "-NonInteractive", "-File", self.script_path]
        if self.dry_run:
            args.append("-DryRun")
        return args

    def execute(self, action: str, context: dict[str, Any]) -> ActionResult:
        payload = json.dumps({"action": action, "context": context}, default=str)
        try:
            completed = subprocess.run(
                self.command(),
                input=payload,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as err:
            raise PowerShellError(f"PowerShell action timed out after {self.timeout}s") from err
        except FileNotFoundError as err:
            raise PowerShellError(
                f"PowerShell executable not found at '{self.command_path}'", retryable=False
            ) from err
        result = parse_powershell_output(completed.stdout)
        if completed.returncode != 0 or result is None or result.get("status") != "ok":
            detail = (result or {}).get("detail") or (completed.stderr or "").strip() or (
                f"exit code {completed.returncode} with no JSON result"
            )
            raise PowerShellError(
                f"PowerShell {action} failed: {detail}", retryable=completed.returncode != 2
            )
        return ActionResult(
            action=action,
            detail=str(result.get("detail") or f"PowerShell completed {action}"),
            output=result.get("output") or {},
        )


def parse_powershell_output(stdout: str) -> dict[str, Any] | None:
    """Return the last JSON object line in ``stdout`` (warnings may precede it)."""
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


class MicrosoftGraphAdapter:
    """Runs checklist actions against Microsoft Graph, one tenant at a time.

    ``resolve(tenant_key)`` returns ``(GraphClient, TenantSettings)``, so the
    same workflow runs across many customer tenants with different licence
    SKUs and usage locations. Every action is idempotent: running it twice
    finds the existing user/licence/state and reports a no-op.
    """

    EXCHANGE_ONLY_ACTIONS = frozenset({"archive_email_mailbox"})
    NON_M365_ACTIONS = frozenset({"provision_hardware", "recover_hardware"})

    def __init__(
        self,
        resolve: Callable[[str | None], Any],
        *,
        exchange_fallback: ActionAdapter | None = None,
        default_sku: str = "O365_BUSINESS_PREMIUM",
    ) -> None:
        self.resolve = resolve
        self.exchange_fallback = exchange_fallback
        self.default_sku = default_sku

    def execute(self, action: str, context: dict[str, Any]) -> ActionResult:
        if action in self.NON_M365_ACTIONS:
            return ActionResult(action=action, detail=f"'{action}' is a PSA/RMM task; no Graph call made")
        if action in self.EXCHANGE_ONLY_ACTIONS:
            if self.exchange_fallback is None:
                from src.graph import GraphError

                raise GraphError(
                    f"'{action}' needs Exchange Online PowerShell; set FLOWOPS_POWERSHELL_PATH",
                    code="ExchangeFallbackMissing",
                    status_code=501,
                )
            result = self.exchange_fallback.execute(action, context)
            return ActionResult(action=action, detail=f"via Exchange Online PowerShell: {result.detail}", output=result.output)
        handler = getattr(self, f"_{action}", None)
        if handler is None:
            raise ValueError(f"No Microsoft Graph implementation for action {action!r}")
        client, tenant = self.resolve(context.get("tenant_key"))
        detail, output = handler(client, tenant, context)
        output = {"tenant": tenant.key, **output}
        return ActionResult(action=action, detail=f"[{tenant.key}] {detail}", output=output)

    @staticmethod
    def _upn(context: dict[str, Any]) -> str:
        return str(context["employee_email"]).strip().lower()

    def _create_user_account(self, client, tenant, context):
        upn = self._upn(context)
        existing = client.find_user(upn)
        if existing:
            return f"user {upn} already exists", {"user_id": existing["id"], "created": False}
        user = client.create_user(
            display_name=context.get("employee_name") or upn,
            user_principal_name=upn,
            department=context.get("department"),
            usage_location=tenant.usage_location,
        )
        return f"created user {upn}", {"user_id": user.get("id"), "created": True}

    def _grant_software_licenses(self, client, tenant, context):
        from src.graph import GraphError, available_units

        upn = self._upn(context)
        user = client.require_user(upn)
        part = context.get("licence_sku") or tenant.licence_sku or self.default_sku
        sku = client.sku(part)
        if any(lic.get("skuId") == sku["skuId"] for lic in user.get("assignedLicenses") or []):
            return f"{part} already assigned to {upn}", {"sku": part, "assigned": False}
        if available_units(sku) <= 0:
            raise GraphError(
                f"no free {part} licences in tenant {tenant.key}; buy more or free one up",
                code="LicenceExhausted",
                status_code=409,
            )
        if not user.get("usageLocation"):
            if not tenant.usage_location:
                raise GraphError(
                    f"{upn} has no usageLocation and tenant {tenant.key} has no default",
                    code="UsageLocationMissing",
                    status_code=400,
                )
            client.update_user(user["id"], {"usageLocation": tenant.usage_location})
        client.assign_licenses(user["id"], add=[sku["skuId"]])
        return f"assigned {part} to {upn}", {"sku": part, "assigned": True}

    def _configure_email_mailbox(self, client, tenant, context):
        upn = self._upn(context)
        user = client.require_user(upn)
        settings = {
            "timeZone": context.get("time_zone") or "UTC",
            "language": {"locale": context.get("language") or "en-US"},
        }
        client.update_mailbox_settings(user["id"], settings)
        return f"configured mailbox for {upn}", {"mailbox_settings": settings}

    def _disable_user_account(self, client, tenant, context):
        upn = self._upn(context)
        user = client.require_user(upn)
        if user.get("accountEnabled") is not False:
            client.update_user(user["id"], {"accountEnabled": False})
        client.revoke_sign_in_sessions(user["id"])
        return f"disabled {upn} and revoked sessions", {"user_id": user["id"]}

    def _revoke_software_licenses(self, client, tenant, context):
        upn = self._upn(context)
        user = client.require_user(upn)
        skus = [lic["skuId"] for lic in user.get("assignedLicenses") or [] if lic.get("skuId")]
        if skus:
            client.assign_licenses(user["id"], remove=skus)
        return f"removed {len(skus)} licence(s) from {upn}", {"removed_sku_ids": skus}


def _truthy(value: str | None) -> bool:
    return (value or "").lower() in ("true", "1", "yes")


def _powershell_from_environment(values: dict[str, str] | Any) -> PowerShellAdapter:
    return PowerShellAdapter(
        command_path=values.get("FLOWOPS_POWERSHELL_PATH", "pwsh"),
        dry_run=_truthy(values.get("FLOWOPS_POWERSHELL_DRY_RUN")),
        script_path=values.get("FLOWOPS_POWERSHELL_SCRIPT") or None,
    )


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
    if name in ("graph", "graph_simulated"):
        from src.tenants import GraphClientFactory

        simulated = name == "graph_simulated"
        has_secret = values.get("FLOWOPS_GRAPH_CLIENT_ID") and values.get("FLOWOPS_GRAPH_CLIENT_SECRET")
        if not simulated and not (values.get("FLOWOPS_GRAPH_ACCESS_TOKEN") or has_secret):
            raise ValueError(
                "graph needs FLOWOPS_GRAPH_CLIENT_ID + FLOWOPS_GRAPH_CLIENT_SECRET "
                "(client credentials) or FLOWOPS_GRAPH_ACCESS_TOKEN"
            )
        fallback: ActionAdapter | None = None
        if simulated:
            fallback = PowerShellAdapter(dry_run=True)
        elif values.get("FLOWOPS_POWERSHELL_PATH") or values.get("FLOWOPS_EXO_APP_ID"):
            fallback = _powershell_from_environment(values)
        return MicrosoftGraphAdapter(
            GraphClientFactory(dict(values), simulated=simulated),
            exchange_fallback=fallback,
            default_sku=values.get("FLOWOPS_GRAPH_DEFAULT_SKU") or "O365_BUSINESS_PREMIUM",
        )
    if name == "webhook":
        url = values.get("FLOWOPS_WEBHOOK_URL")
        if not url:
            raise ValueError("FLOWOPS_WEBHOOK_URL is required for webhook")
        return GenericWebhookAdapter(
            url=url,
            timeout=float(values.get("FLOWOPS_WEBHOOK_TIMEOUT", "10.0")),
            secret=values.get("FLOWOPS_WEBHOOK_SECRET") or None,
        )
    if name == "powershell":
        return _powershell_from_environment(values)
    if name == "rpa":
        from src.rpa import rpa_from_env

        adapter = rpa_from_env(values)
        if adapter is None:
            raise ValueError("FLOWOPS_RPA_WEBHOOK_URL is required for rpa")
        return adapter
    raise ValueError(f"Unsupported FLOWOPS_ACTION_ADAPTER: {name!r}")


def action_for_step(step: str) -> str:
    """Return the stable action name for a checklist step."""
    try:
        return ACTION_BY_STEP[step]
    except KeyError as error:
        raise ValueError(f"No automation action is mapped for checklist step: {step!r}") from error
