# Security and credentials runbook

## Storage and rotation

Do not store raw credentials in database config JSON or source control. Tenant
rows store a `credential_ref`; runtime settings resolve names such as
`FLOWOPS_TENANT_<REF>_GRAPH_CLIENT_SECRET` from the environment/secret manager.
Rotate Graph, ConnectWise, MCP, webhook, and RPA secrets by updating the
secret store and restarting workers as needed. Verify the old credential is
revoked after a successful smoke test.

## Least privilege

Use the smallest Microsoft Graph scopes, tenant-specific ConnectWise keys,
dedicated webhook secrets, and a dedicated MCP bearer token. Keep
`FLOWOPS_MCP_ALLOW_WRITES=false` unless an explicitly reviewed agent needs
write tools.

## Authentication and audit

Local demos default to simulated adapters and disabled auth. Production-like
deployments should set `FLOWOPS_AUTH_REQUIRED=true` and
`FLOWOPS_UI_AUTH_REQUIRED=true`, configure Entra/bearer validation, and use
secure session cookies. Every operator action should retain actor attribution,
correlation ID, and an audit/execution log entry.
