# Tenant Fencing and Worker Recovery Audit (codex/project-improvements)

Scope: the multi-tenant MSP boundary (tenant fencing) and the durable job/lease
worker recovery paths. The workforce module was already fully fenced; this pass
closes the comparable gaps in the core ticket/onboarding plane and documents
what remains open.

## What was found

Findings are grouped by severity for an outside actor. "Local actor" means the
unauthenticated `local-development` actor present when `FLOWOPS_AUTH_REQUIRED`
is false; development keeps its current, global behavior.

### Enforced before this pass (no gaps)

- All workforce endpoints scope every query by the actor's `tenant_key`
  (`src/routes/workforce.py`); tenant-keyed non-admin bearer actors without a
  tenant are rejected; tenant existence/active state is validated.
- Automation callbacks (`/workforce/automation/callbacks/*`) and integration
  endpoints authenticate via HMAC key and require an explicit tenant key;
  cross-tenant callbacks return 404.
- The job/outbox/run/lease transitions are conditionally updated and fenced on
  `lease_owner_token`, so a stale worker cannot commit after another worker
  took over (`src/jobs.py`, `src/outbox.py`, `src/scheduler.py`,
  `src/workforce_worker.py`).

### Closed by this pass (previously reachable without an enforced tenant)

| Area | Gap | Fix |
|---|---|---|
| `GET /tickets`, `GET /tickets/{id}` | Any caller could read any tenant's ticket; the tenant scoping came from a caller-supplied query param. | `_actor_from_header` is now a dependency: a tenant-bearing actor's list is filtered to its tenant and single reads return 404 outside it (`src/api.py`). |
| `POST /tickets` (operator) | Ticket tenant was stamped from the caller payload. | A tenant-capable actor's tenant is stamped into the ticket; a conflicting payload tenant is rejected with 404. |
| `GET/POST /onboarding-requests/{id}`(+`/status`) | Bare-id fetch, no tenant check. | List is scoped; detail/status return 404 outside the actor's tenant; creation stamps/enforces the actor tenant before the idempotency reservation. |
| PSA sync (`POST /tickets/{id}/psa-sync`) | Any operator could sync any tenant's ticket into that ticket's PSA tenant. | Tenant mismatch returns 404 (`src/routes/tenants.py`). |
| ChatOps approve/reject links | Signed single-use tokens bound only request/item ids, not the tenant. | Tokens now embed `tenant_key`; verification rejects a token whose tenant does not match the request's row with 404 while legacy tenant-less tokens keep working (`src/chatops.py`, `src/api.py`, UI minting passes the record tenant). |
| Connector administration (`/connectors`) | Connector listing was unauthenticated; tenant admins could inspect or mutate another tenant's connector, and a webhook payload could claim another tenant. | Listing now requires admin access and scopes by actor tenant; create/update stamp the actor tenant, mutations 404 on cross-tenant IDs, polling is tenant-scoped, and webhook events inherit the connector tenant (`src/routes/connectors.py`, `src/connectors.py`). |
| Trigger-rule updates (`/trigger-rules`) | A tenant admin could omit the optional tenant on PUT and promote a rule to global scope. | Updates now preserve the authenticated tenant; the regression test proves the tenant field cannot be cleared. |

New regression tests: `tests/test_tenant_fencing_api.py`
(tenant visibility, stamping, 404-on-mismatch, legacy tokens) and
`tests/test_worker_recovery.py` (crashed-lease takeover, stale-worker fencing,
retry budget after recovery).

### Documented-but-open (deliberately not changed here)

1. Workflow runs and jobs have no `tenant_key` column. The follow-up API fix
   scopes runs through their onboarding request and onboarding jobs through
   `resource_id`; records without a resolvable request are hidden from tenant
   actors. Other request, step, and operator surfaces still need a separate
   endpoint-by-endpoint review.
2. `notification_outbox`, execution logs, and the analytics/report views are
   operator-plane surfaces; their tenant filter is caller-supplied and
   presentational.
3. Legacy scheduled/webhook triggers (`src/routes/triggers.py`) still use
   global event-source and trigger records without `tenant_key`. Their CRUD,
   delivery payloads, operator pages, and metrics need a separate migration
   and tenant-scope review. The connector and newer trigger-rule APIs are now
   fenced as described above.
4. MCP tools (`src/mcp_server.py`) still trust caller-supplied tenant args.
5. When `FLOWOPS_AUTH_REQUIRED=false` the local actor intentionally stays
   global — that keeps local development and the portfolio demo unchanged.

## Worker recovery verification

Verified against the lease-fenced queue with the new recovery drill tests:

- **Crash while running**: a claimed job whose worker dies is re-queued by the
  next `claim_job` once the lease lapses; the recovery worker completes it
  (`attempts == 2`), and the checklist item reaches `completed` exactly once.
- **Late stale worker**: the crashed worker's `complete_job` is rejected
  because its `lease_owner_token` no longer matches; no state is overwritten.
- **Recovered attempt failing**: the normal exponential-backoff retry
  (`retry_scheduled`, capped budget, dead-letter after exhaustion) still
  applies to the recovery attempt.

Not exercised here (kept from the previous plan): Postgres
`FOR UPDATE SKIP LOCKED` takeover running live (covered by
`tests/test_jobs_postgres.py`, which requires a PG URL), and a crash window
between a side-effect's external call and the commit — idempotency is protected
by the per-item unique key plus the `already_completed` replay finalize.
