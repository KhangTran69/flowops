"""Run the checked-in Exchange Online script and parse its JSON output."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional

from src.actions import ActionResult

DEFAULT_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "exchange" / "Set-MailboxForwarding.ps1"


class PowerShellScriptAdapter:
    def __init__(self, script: Optional[Path] = None, command_path: str = "pwsh", timeout: float = 30.0, dry_run: bool = False) -> None:
        self.script = Path(script) if script else DEFAULT_SCRIPT
        self.command_path = command_path
        self.timeout = timeout
        self.dry_run = dry_run

    def execute(self, action: str, context: Mapping[str, Any]) -> ActionResult:
        email = str(context.get("employee_email") or context.get("user_principal_name") or "")
        if not email:
            raise ValueError("employee_email is required for the Exchange action")
        if self.dry_run:
            return ActionResult(action=action, detail="simulated Exchange script", output={"userPrincipalName": email})
        import subprocess
        result = subprocess.run(
            [self.command_path, "-NoProfile", "-NonInteractive", "-File", str(self.script),
             "-UserPrincipalName", email,
             "-ForwardTo", str(context.get("forward_to") or ""),
             "-AutoReply", str(context.get("auto_reply") or "")],
            capture_output=True, text=True, timeout=self.timeout, check=True,
        )
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            raise RuntimeError("Exchange script returned no output")
        try:
            output = json.loads(lines[-1])
        except ValueError as error:
            raise RuntimeError(f"Exchange script returned invalid JSON: {lines[-1][:200]}") from error
        return ActionResult(action=action, detail="Exchange mailbox updated", output=output)
