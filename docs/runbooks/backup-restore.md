# Backup and restore rehearsal

Proves the data-safety release gate (ROADMAP 2b): a FlowOps PostgreSQL
database can be dumped, restored into a clean server, and put back into
service by replaying the migration chain — with row counts reconciled and
the wall-clock window measured, not guessed.

The rehearsal runs automatically in CI (the `docker-postgres` job step
"Backup-restore rehearsal") and uploads its timings as the dated evidence
artifact `backup-restore-timings`. Run only against an isolated, explicitly
disposable synthetic compose stack with no concurrent API traffic. The script
seeds fixtures and must never be pointed at a real tenant database:

```bash
FLOWOPS_DISPOSABLE_REHEARSAL=1 ./scripts/backup_restore_rehearsal.sh
```

**What the script does, in order** (all against a running compose stack):

1. **Quiesce writes.** The writer services (worker, scheduler, timers,
   outbox, workforce-outbox) are paused so the dump is a deterministic
   point-in-time copy; the web app stays up. They resume automatically in
   the script's exit trap, even on failure.
2. **Guarantee non-trivial data.** The demo seed runs when present
   (`python -m scripts.seed_demo`), plus three `rehearsal-000N` fixture
   tickets inserted through the normal models so the restored copy can be
   spot-checked verbatim.
3. **Dump** — `pg_dump` in plain SQL format.
4. **Restore** — the dump is piped into a throwaway `postgres:16-alpine`
   container (`flowops-rehearsal-pg`) attached to the same compose network,
   restored with `ON_ERROR_STOP=1` so a torn restore fails loudly.
5. **Replay the migration chain on restored data** — this is the ROADMAP
   requirement that migration 0016 (manual-ticket fields) is rehearsed on a
   restored copy before it ever runs on real data. The restored copy is
   explicitly disposable, so
   `scripts/check_manual_ticket_migration.py` runs its full roundtrip
   against it: upgrade to head, insert a fixture ticket, downgrade to
   `0015_event_source_scope_rotation`, assert the optional columns dropped
   but the legacy row survived, upgrade to head, assert the roundtrip
   semantics. That replays 0016 and everything after it on restored data.
6. **Reconcile** — row counts for tickets, execution logs, workflow runs,
   checklist items, notification outbox, onboarding requests, workflow jobs,
   workforce audit events, and tenants must match the source exactly;
   `alembic_version` must match on both sides; the three rehearsal fixture
   tickets must exist verbatim in the restored copy.
7. **Measure** — per-stage wall time is printed and written to
   `backup-restore-timings.txt` (CI uploads it as an artifact).

## Measured window (2026-11-28, local compose stack)

Recorded on the developer machine (Apple Silicon, local Docker) against the
main-branch stack; CI re-measures on every run of the `docker-postgres` job
and uploads the timings as the `backup-restore-timings` artifact. At the
demo dataset's size the window is dominated by fixed costs (container start,
migration replay), not row count:

| Stage | Measured |
| --- | --- |
| Dump (`pg_dump`, writers paused) | 0.2 s |
| Restore into clean `postgres:16-alpine` | 1.7 s |
| Migration replay on restored copy (0016 roundtrip) | 1.6 s |
| Reconcile (9 tables + version + spot checks) | 2.4 s |
| **Total (excluding fixture prep)** | **5.9 s** |

Both failure gates of the script were proven to bite on planted violations
(2026-11-28): a row written to the source after the dump produces
`MISMATCH tickets: source 4 vs restored 3` and exit 1, and an unexpected
fixture row in the restored copy fails the verbatim spot check the same way.

## Manual primitives (what the script automates)

For an operator without the script — the same rehearsal by hand:

```bash
# 1. Quiesce writes (resume them after step 5)
docker compose pause worker scheduler timers outbox workforce-outbox

# 2. Dump (plain SQL, consistent snapshot)
docker compose exec -T postgres pg_dump -U flowops --no-owner --no-privileges flowops > flowops-backup.sql

# 3. Start a clean target and restore
docker run -d --name flowops-rehearsal-pg --network <compose_network> \
  -e POSTGRES_DB=flowops -e POSTGRES_USER=flowops -e POSTGRES_PASSWORD=flowops \
  postgres:16-alpine
docker exec -i flowops-rehearsal-pg psql -U flowops -d flowops \
  -v ON_ERROR_STOP=1 -q < flowops-backup.sql

# 4. Rehearse the migration chain on the restored copy (disposable!)
docker compose exec -T \
  -e FLOWOPS_DISPOSABLE_MIGRATION_CHECK=1 \
  -e FLOWOPS_DATABASE_URL=postgresql+psycopg://flowops:flowops@flowops-rehearsal-pg:5432/flowops \
  app python scripts/check_manual_ticket_migration.py

# 5. Reconcile and resume
docker exec flowops-rehearsal-pg psql -U flowops -d flowops -tAc "SELECT count(*) FROM tickets"
docker compose exec -T postgres psql -U flowops -d flowops -tAc "SELECT count(*) FROM tickets"
docker compose unpause worker scheduler timers outbox workforce-outbox
docker rm -f flowops-rehearsal-pg
```

The compose network name is the project's default network
(`<project>_default`); the script resolves it from the running postgres
container. Never run the disposable seed or migration downgrade checker on production.
For an actual incident, use a separately authorized restore into a fresh
isolated target, verify the backup checksum, schema and application checks,
then switch traffic only after approval. Keep the old database isolated for
forensic and rollback review.

## Recovery philosophy

- **Recovery point objective**: the last successful dump. The dump is a
  consistent snapshot taken while writers are paused, so a restored copy is
  exactly the state at dump time.
- **Recovery time objective**: restore + migration replay + reconcile —
  the measured window above. Scale it with data volume: the demo dataset is
  small, so the window is dominated by fixed costs (container start,
  migration replay), not row count.
- **What is NOT covered**: volumes outside PostgreSQL (Grafana dashboards,
  Prometheus TSDB) and anything in external systems. Those are re-provisionable;
  the FlowOps database is the state that matters.

## Additional CI integrity checks

Before the optional-column migration roundtrip, the rehearsal compares
content digests of all nine selected tables, not only counts. A deliberately
failing SQL transaction on the disposable restored server must exit nonzero
and leave no probe table. The recovery artifact records both assertions and
elapsed time separately. This is synthetic transaction and restoration
integrity evidence, not a production disaster recovery exercise.

If a release or migration fails, stop writes, retain the original backup and
image identifiers, review the migration state, and validate a separate
restored copy. Never attempt a destructive downgrade on the live database.
