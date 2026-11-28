# Shared authenticated request admission

PostgreSQL deployments use one atomic fixed-window counter per hashed
authenticated actor and tenant, or authenticated webhook identity. The
quota transaction commits before the operation; failed operations consume
admission quota. All processes must point to the same database and use the
same configured FLOWOPS_RATE_LIMIT_PER_MINUTE (1–100000 outside development).
Database time defines the minute boundary; a 429 includes remaining seconds.
The fixed-window algorithm permits a boundary burst. SQLite retains the
existing in-process development limiter only; protected startup rejects it.
The shared limiter returns 503 on database/lock errors and never falls back
to local counters. No raw subjects or keys are written to the counter table.

Apply Alembic revision 0018 before starting protected API replicas. Keep the
outbox retention worker enabled for cleanup; it removes old buckets in
bounded batches. Roll back only after draining the new API version. Dropping
the counter table resets quotas and is not a safe live rollback.

Scrape per-process flowops_rate_limit_decisions_total (allowed, denied,
unavailable) and the existing HTTP status counters. Aggregate counters across
replicas; a sustained unavailable rate means check database pool, lock
contention and migration state. Do not bypass admission to recover. This
controls authenticated operations, not unauthenticated brute-force or network
DDoS; configure ingress limits, request-size caps and trusted proxy policy.
Staging acceptance still requires multi-host representative traffic,
capacity sizing and alert delivery verification.

The PostgreSQL regression suite also holds an independent quota-row lock,
asserts admission fails closed at the lock timeout, releases it, and verifies
that admission resumes without counting the failed attempt. This is a
controlled contention fault, not a full database outage or host-loss test.
