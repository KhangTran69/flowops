"""MCP (Model Context Protocol) server exposing FlowOps to AI agents.

Claude Desktop / Claude Code (or any MCP client) launches this over stdio::

    python -m src.mcp_server

Every tool calls the FlowOps REST API rather than the database, so the
agent goes through the same authentication, role checks, rate limits,
idempotency and audit log as a human operator. Mutating tools are disabled
unless ``FLOWOPS_MCP_ALLOW_WRITES=true``, and none of them can approve a
workflow step: approval stays with a human.

Environment:
    FLOWOPS_API_URL          default http://127.0.0.1:8000
    FLOWOPS_MCP_API_TOKEN    bearer token for the FlowOps API (optional locally)
    FLOWOPS_MCP_ALLOW_WRITES enable create/retry tools (default false)
"""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer


class FlowOpsToolError(RuntimeError):
    pass


class FlowOpsTools:
    """Plain-Python implementations of the MCP tools (easy to unit test)."""

    def __init__(self, http_client: httpx.Client, *, allow_writes: bool = False) -> None:
        self.http = http_client
        self.allow_writes = allow_writes

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.http.request(method, path, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise FlowOpsToolError(f"FlowOps API {method} {path} -> {response.status_code}: {detail}")
        return response.json()

    def _require_writes(self, tool: str) -> None:
        if not self.allow_writes:
            raise FlowOpsToolError(f"{tool} is a write tool; set FLOWOPS_MCP_ALLOW_WRITES=true to enable it")

    def list_tickets(self, status: str | None = None, queue: str | None = None, limit: int = 20) -> list[dict]:
        tickets = self._call("GET", "/tickets", params={"limit": 500})
        filtered = [
            t for t in tickets
            if (status is None or t["status"] == status) and (queue is None or t.get("queue") == queue)
        ]
        return filtered[: max(1, min(limit, 100))]

    def get_ticket(self, ticket_id: str) -> dict:
        return self._call("GET", f"/tickets/{ticket_id}")

    def triage_ticket(self, ticket_id: str) -> dict:
        """Read-only: returns summary, KB matches and validated suggestions."""
        return self._call("POST", f"/tickets/{ticket_id}/ai-triage")

    def list_pending_approvals(self) -> list[dict]:
        pending = []
        for request in self._call("GET", "/onboarding-requests"):
            for item in request.get("checklist", []):
                if item.get("requires_approval") and item.get("status") == "pending":
                    pending.append({
                        "request_id": request["id"],
                        "item_id": item["id"],
                        "employee_email": request["employee_email"],
                        "request_type": request["request_type"],
                        "tenant_key": request.get("tenant_key"),
                        "step": item["step"],
                        "queue": item["queue"],
                    })
        return pending

    def request_onboarding(
        self,
        request_type: str,
        employee_name: str,
        employee_email: str,
        department: str,
        requester_id: int,
        tenant_key: str | None = None,
        start_date: str | None = None,
    ) -> dict:
        """Create an onboarding/offboarding request; its approval steps wait for a human."""
        self._require_writes("request_onboarding")
        if not tenant_key or not tenant_key.strip():
            raise FlowOpsToolError("request_onboarding is a write tool and requires tenant_key")
        body = {
            "request_type": request_type,
            "employee_name": employee_name,
            "employee_email": employee_email,
            "department": department,
            "requester_id": requester_id,
            "tenant_key": tenant_key,
            "start_date": start_date,
        }
        key = "mcp-" + uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(body, sort_keys=True)).hex
        return self._call(
            "POST", "/onboarding-requests",
            json={k: v for k, v in body.items() if v is not None},
            headers={"Idempotency-Key": key},
        )

    def list_failed_jobs(self, limit: int = 20) -> list[dict]:
        return self._call("GET", "/workflow-jobs", params={"status": "failed", "limit": max(1, min(limit, 200))})

    def retry_job(self, job_id: int) -> dict:
        self._require_writes("retry_job")
        return self._call("POST", f"/workflow-jobs/{job_id}/retry")

    @staticmethod
    def _resource_key(key: str) -> str:
        """Keep MCP identifiers inside a single API path segment."""
        if key in (".", "..") or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", key):
            raise FlowOpsToolError("Invalid resource key: use letters, digits, dots, underscores or hyphens.")
        return key

    def list_workflow_definitions(self) -> list[dict]:
        """List workflow definitions; requires a global administrator API credential."""
        return self._call("GET", "/workflow-definitions")

    def get_workflow_definition(self, key: str) -> dict:
        """Inspect one workflow definition and its immutable versions."""
        return self._call("GET", f"/workflow-definitions/{self._resource_key(key)}")

    def list_workflow_runs(
        self, status: str | None = None, tenant: str | None = None,
        definition: str | None = None, limit: int = 20, offset: int = 0,
    ) -> list[dict]:
        """List authorized workflow runs with optional filters and pagination."""
        params = {"status": status, "tenant": tenant, "definition": definition,
                  "limit": max(1, min(limit, 200)), "offset": max(0, offset)}
        return self._call("GET", "/workflow-runs", params={k: v for k, v in params.items() if v is not None})

    def get_workflow_run(self, run_id: int) -> dict:
        """Inspect one authorized workflow run, including checklist and attempts."""
        return self._call("GET", f"/workflow-runs/{run_id}")

    def list_triggers(self) -> list[dict]:
        """List trigger configuration; requires a global administrator API credential."""
        return self._call("GET", "/triggers")

    def get_trigger(self, key: str) -> dict:
        """Inspect a trigger configuration without firing it."""
        return self._call("GET", f"/triggers/{self._resource_key(key)}")

    def automation_kpis(self, days: int = 30) -> dict:
        return self._call("GET", "/reports/summary", params={"days": days})

    def tenant_drift_report(self, tenant: str | None = None) -> dict:
        return self._call("POST", "/tenants/drift-check", params={"tenant": tenant} if tenant else None)


TOOL_NAMES = (
    "list_tickets", "get_ticket", "triage_ticket", "list_pending_approvals", "request_onboarding",
    "list_failed_jobs", "retry_job", "automation_kpis", "tenant_drift_report",
    "list_workflow_definitions", "get_workflow_definition", "list_workflow_runs", "get_workflow_run",
    "list_triggers", "get_trigger",
)


def build_server(tools: FlowOpsTools) -> MCPServer:
    server = MCPServer(
        "flowops",
        instructions=(
            "FlowOps is an MSP service-desk automation platform. Use read tools to inspect "
            "tickets, workflow definitions and runs, triggers, failed jobs, KPIs and tenant drift. "
            "Triage suggestions are advisory; "
            "never claim a step was approved, approvals are done by a human operator."
        ),
    )
    for name in TOOL_NAMES:
        method = getattr(tools, name)
        server.add_tool(method, name=name, description=(method.__doc__ or name).strip().splitlines()[0])
    return server


def server_from_environment() -> MCPServer:
    headers = {}
    token = os.environ.get("FLOWOPS_MCP_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    client = httpx.Client(
        base_url=os.environ.get("FLOWOPS_API_URL", "http://127.0.0.1:8000"),
        headers=headers,
        timeout=30.0,
    )
    allow = os.environ.get("FLOWOPS_MCP_ALLOW_WRITES", "").lower() in ("1", "true", "yes")
    return build_server(FlowOpsTools(client, allow_writes=allow))


if __name__ == "__main__":
    server_from_environment().run()
