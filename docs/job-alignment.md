# Automation Engineer job alignment

FlowOps is a portfolio implementation of an MSP automation platform. The
following map is useful when reviewing the project or presenting it in an
interview.

| Role expectation | FlowOps evidence |
|---|---|
| Service desk and onboarding automation | Ticket routing, onboarding/offboarding checklists, DAG orchestration, approval gates |
| Power Automate | [`flows/power-automate/`](../flows/power-automate/) build guide and HTTP-trigger integration |
| Rewst | [`flows/rewst/`](../flows/rewst/) platform blueprint and migration approach |
| API/PSA integrations | Microsoft Graph, Exchange Online PowerShell, ConnectWise Manage, n8n exports |
| MCP and Claude | MCP tools with read/write separation and Claude provider support |
| SQL and KPIs | PostgreSQL function, reporting views, dashboard, tenant-scoped reports |
| Reliability | Fenced idempotency, leases, retries, dead-letter queues, correlation IDs |
| Multi-tenant/GDAP | Tenant registry, credential references, licence baselines, drift checks |
| Documentation | Operator runbooks, integration guide, portfolio demo, and platform build guides |

The live third-party integrations are credential-gated and the default adapter
is simulated. That makes the repository safe to run locally while preserving
the same API contracts, error paths, and tests used by the real adapters.
