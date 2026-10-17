# Multi-tenancy plan (deferred)

Multi-tenant support is a schema-wide change, not a single endpoint. The safe
sequence is:

1. Add nullable `tenant_id` to tickets, requests, definitions, runs, timers,
   jobs, outbox rows, execution logs and delivery ledgers.
2. Add a `tenants` table with per-tenant settings and encrypted credential
   references.
3. Backfill existing rows to a `default` tenant, then make the column
   non-nullable.
4. Add tenant-scoped unique constraints and indexes.
5. Resolve the tenant from the authenticated actor, never from a request body.
6. Add a cross-tenant admin role for reporting only; normal operators are
   always scoped.
7. Run one scheduler/timer tick per tenant or include tenant in every lease
   query.
8. Add tests proving tenant A cannot read or mutate tenant B.

This is intentionally deferred: it touches every table and auth boundary and
needs its own migration/rollback plan.
