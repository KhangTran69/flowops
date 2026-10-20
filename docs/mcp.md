# MCP connectors

FlowOps exposes an MCP server for Claude Desktop, Claude Code, or another MCP
client. Start it with `python -m src.mcp_server`.

## Tools

The server registers these tools from [`src/mcp_server.py`](../src/mcp_server.py):

- `list_tickets`, `get_ticket`, and `triage_ticket`
- `list_pending_approvals`
- `request_onboarding`
- `list_failed_jobs` and `retry_job`
- `automation_kpis`
- `tenant_drift_report`

## Claude Desktop configuration

```json
{
  "mcpServers": {
    "flowops": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "src.mcp_server"],
      "cwd": "/absolute/path/to/flowops",
      "env": {
        "FLOWOPS_API_URL": "http://127.0.0.1:8000",
        "FLOWOPS_MCP_API_TOKEN": "use-a-secret-token",
        "FLOWOPS_MCP_ALLOW_WRITES": "false"
      }
    }
  }
}
```

Write tools are disabled unless `FLOWOPS_MCP_ALLOW_WRITES=true`. Even when
enabled, the MCP layer cannot approve checklist items automatically; approval
remains a human/operator action. Use a dedicated bearer token, restrict the
API role, and keep the MCP server on a trusted host.
