"""Minimal official MCP server exposing FlowOps tools.

Install the optional SDK:
    pip install -r requirements-mcp.txt

Run (stdio):
    python -m src.mcp_server

Configure:
    FLOWOPS_API_URL=http://127.0.0.1:8000
    FLOWOPS_API_TOKEN=...
    FLOWOPS_CHATOPS_SECRET=...   # for request_approval links
"""

from __future__ import annotations

import os
from typing import Any

import httpx

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as error:  # pragma: no cover - optional dependency
    raise SystemExit("Install the MCP SDK: pip install -r requirements-mcp.txt") from error

from src import chatops

mcp = FastMCP("FlowOps")
BASE = os.environ.get("FLOWOPS_API_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.environ.get("FLOWOPS_API_TOKEN", "")


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}


async def _request(method: str, path: str, **kwargs: Any) -> Any:
    async with httpx.AsyncClient(base_url=BASE, timeout=20) as client:
        response = await client.request(method, path, headers=_headers(), **kwargs)
        response.raise_for_status()
        return response.json()


@mcp.tool()
async def list_tickets(status: str = "open", limit: int = 20) -> list[dict[str, Any]]:
    """List FlowOps tickets, optionally filtered by status."""
    data = await _request("GET", "/tickets", params={"status": status})
    return (data if isinstance(data, list) else data.get("items", []))[: max(1, min(limit, 100))]


@mcp.tool()
async def triage_ticket(ticket_id: str) -> dict[str, Any]:
    """Run AI triage for a ticket. This never mutates queue, priority or status."""
    return await _request("POST", f"/tickets/{ticket_id}/ai-triage")


@mcp.tool()
def request_approval(request_id: int, item_id: int, action: str = "approve") -> dict[str, str]:
    """Generate a signed, single-use ChatOps approval link for a workflow step."""
    if action not in ("approve", "reject"):
        raise ValueError("action must be approve or reject")
    url = chatops.generate_token(request_id, item_id, action)
    return {"url": f"/webhooks/actions/{action}?token={url}", "action": action, "expires_in_seconds": "86400"}


if __name__ == "__main__":
    mcp.run()
