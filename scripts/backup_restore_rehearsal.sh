#!/usr/bin/env bash
# Backup/restore rehearsal for a running FlowOps compose stack (ROADMAP 2b).
#
# Dumps the stack's PostgreSQL database, restores it into a throwaway
# postgres:16-alpine container on the same compose network, replays the
# manual-ticket migration chain on the restored copy
# (scripts/check_manual_ticket_migration.py — explicitly disposable DB only),
# reconciles row counts and spot checks between source and restored copy, and
# prints per-stage wall time. Writes backup-restore-timings.txt as the dated
# evidence artifact.
#
# Usage: from the repo root of a RUNNING compose stack:
#   ./scripts/backup_restore_rehearsal.sh
#
# The rehearsal never writes to the source stack: writer services (worker,
# scheduler, timers, outbox, workforce-outbox) are paused for the dump so the
# reconciliation is deterministic, and resumed in the EXIT trap. The app
# service stays up.
set -euo pipefail

REHEARSAL_CONTAINER="flowops-rehearsal-pg"
REHEARSAL_HOST_PORT="${FLOWOPS_REHEARSAL_PG_PORT:-}"
WORK_DIR="$(mktemp -d)"
TIMINGS_FILE="${FLOWOPS_REHEARSAL_TIMINGS_FILE:-backup-restore-timings.txt}"
WRITER_SERVICES=(worker scheduler timers outbox workforce-outbox)
TABLES=(tickets execution_logs workflow_runs checklist_items notification_outbox onboarding_requests workflow_jobs workforce_audit_events tenants)
declare -a STAGE_NAMES=()
declare -a STAGE_SECONDS=()

now() { python3 -c 'import time; print(f"{time.monotonic():.3f}")'; }

stage() {
  local name="$1" start="$2" end
  end="$(now)"
  python3 - "$name" "$start" "$end" <<'PY'
import sys
name, start, end = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
print(f"  {name}: {end - start:.1f}s")
PY
  STAGE_NAMES+=("$name")
  STAGE_SECONDS+=("$(python3 - "$start" "$end" <<'PY'
import sys
print(f"{float(sys.argv[2]) - float(sys.argv[1]):.1f}")
PY
)")
}

cleanup() {
  local code=$?
  docker rm -f "$REHEARSAL_CONTAINER" >/dev/null 2>&1 || true
  docker compose unpause "${WRITER_SERVICES[@]}" >/dev/null 2>&1 || true
  rm -rf -- "$WORK_DIR"
  if [[ $code -ne 0 ]]; then
    echo "Rehearsal FAILED (exit ${code}); writer services resumed and rehearsal container removed." >&2
  fi
  exit "$code"
}
trap cleanup EXIT

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ "$(docker compose ps --format '{{.State}}' postgres 2>/dev/null || true)" != "running" ]]; then
  echo "No running 'postgres' compose service. Start the stack first: docker compose up -d --wait" >&2
  exit 1
fi

GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "FlowOps backup/restore rehearsal — ${STAMP} — commit ${GIT_SHA}"

# ---------------------------------------------------------------------------
echo "Preparing non-trivial data on the source stack"
# The demo seed (when present in the image) fills every dashboard; the
# rehearsal fixtures add rows this script can spot-check verbatim afterwards.
if docker compose exec -T app python -c "import scripts.seed_demo" >/dev/null 2>&1; then
  docker compose exec -T app python -m scripts.seed_demo
else
  echo "  (seed module not in this image yet; rehearsal fixtures only)"
fi
docker compose exec -T app python - <<'PY'
from datetime import timedelta

from src import db

session = db.SessionLocal()
try:
    for index in (1, 2, 3):
        ticket_id = f"rehearsal-{index:04d}"
        existing = session.get(db.TicketRecord, ticket_id)
        if existing is not None:
            continue
        session.add(db.TicketRecord(
            ticket_id=ticket_id, requester_id=7,
            title=f"Backup rehearsal fixture {index}", status="open",
            priority="normal", tenant_key="demo",
            requester_email="rehearsal@example.test",
        ))
        session.add(db.ExecutionLogRecord(
            run_id=f"rehearsal-{index:04d}", action="classify_route",
            status="success", ticket_id=ticket_id, duration_ms=10 + index,
            detail="Backup/restore rehearsal fixture.", actor_subject="rehearsal",
        ))
    session.commit()
    print("  rehearsal fixtures present")
except Exception:
    session.rollback()
    raise
finally:
    session.close()
PY

echo "Pausing writer services so the dump is deterministic"
docker compose pause "${WRITER_SERVICES[@]}"

# ---------------------------------------------------------------------------
echo "Stage 1/4: pg_dump the source database"
START="$(now)"
docker compose exec -T postgres pg_dump -U flowops --no-owner --no-privileges flowops > "${WORK_DIR}/dump.sql"
[[ -s "${WORK_DIR}/dump.sql" ]]
stage "dump" "$START"

echo "Stage 2/4: restore into a clean postgres:16-alpine container"
COMPOSE_NETWORK="$(docker inspect "$(docker compose ps -q postgres)" \
  --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}')"
START="$(now)"
docker run -d --name "$REHEARSAL_CONTAINER" --network "$COMPOSE_NETWORK" \
  -e POSTGRES_DB=flowops -e POSTGRES_USER=flowops -e POSTGRES_PASSWORD=flowops \
  ${REHEARSAL_HOST_PORT:+-p "127.0.0.1:${REHEARSAL_HOST_PORT}:5432"} \
  postgres:16-alpine >/dev/null
for _ in $(seq 1 60); do
  if docker exec "$REHEARSAL_CONTAINER" pg_isready -U flowops -d flowops >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$REHEARSAL_CONTAINER" pg_isready -U flowops -d flowops >/dev/null
docker exec -i "$REHEARSAL_CONTAINER" psql -U flowops -d flowops \
  -v ON_ERROR_STOP=1 -q < "${WORK_DIR}/dump.sql" > /dev/null
stage "restore" "$START"

echo "Stage 3/4: replay the migration chain on the restored copy"
echo "  (scripts/check_manual_ticket_migration.py: 0016 down/up roundtrip on restored data)"
START="$(now)"
# Images built before scripts/ was copied into them (PR #71) still carry
# alembic.ini and migrations/, so copy the checker in and run it from /app.
docker compose cp scripts/check_manual_ticket_migration.py \
  app:/tmp/check_manual_ticket_migration.py
docker compose exec -T \
  -e FLOWOPS_DISPOSABLE_MIGRATION_CHECK=1 \
  -e FLOWOPS_DATABASE_URL="postgresql+psycopg://flowops:flowops@${REHEARSAL_CONTAINER}:5432/flowops" \
  app python /tmp/check_manual_ticket_migration.py
stage "migration_rehearsal" "$START"

echo "Stage 4/4: reconcile row counts and spot checks"
START="$(now)"
source_count() {
  docker compose exec -T postgres psql -U flowops -d flowops -tAc "SELECT count(*) FROM $1"
}
restored_count() {
  docker exec "$REHEARSAL_CONTAINER" psql -U flowops -d flowops -tAc "SELECT count(*) FROM $1"
}
FAILED=0
for table in "${TABLES[@]}"; do
  src="$(source_count "$table")"
  dst="$(restored_count "$table")"
  if [[ "$src" != "$dst" ]]; then
    echo "  MISMATCH ${table}: source ${src} vs restored ${dst}" >&2
    FAILED=1
  else
    echo "  ok ${table}: ${src} rows"
  fi
done
source_version="$(docker compose exec -T postgres psql -U flowops -d flowops -tAc "SELECT version_num FROM alembic_version")"
restored_version="$(docker exec "$REHEARSAL_CONTAINER" psql -U flowops -d flowops -tAc "SELECT version_num FROM alembic_version")"
[[ "$source_version" == "$restored_version" ]] || { echo "MISMATCH alembic_version: ${source_version} vs ${restored_version}" >&2; FAILED=1; }
echo "  ok alembic_version: ${restored_version}"
restored_fixtures="$(docker exec "$REHEARSAL_CONTAINER" psql -U flowops -d flowops -tAc \
  "SELECT count(*) FROM tickets WHERE ticket_id LIKE 'rehearsal-%'")"
[[ "$restored_fixtures" == "3" ]] || { echo "MISMATCH rehearsal fixtures in restored copy: ${restored_fixtures}" >&2; FAILED=1; }
echo "  ok spot check: 3 rehearsal fixture tickets restored verbatim"
[[ "$FAILED" == 0 ]] || { echo "Reconciliation failed." >&2; exit 1; }
stage "reconcile" "$START"

TOTAL="$(python3 - "${STAGE_SECONDS[@]}" <<'PY'
import sys
print(f"{sum(float(value) for value in sys.argv[1:]):.1f}")
PY
)"
{
  echo "FlowOps backup/restore rehearsal"
  echo "timestamp_utc: ${STAMP}"
  echo "commit: ${GIT_SHA}"
  echo "result: PASS"
  echo "tables_reconciled: ${TABLES[*]}"
  echo "alembic_version_after_replay: ${restored_version}"
  for index in "${!STAGE_NAMES[@]}"; do
    echo "${STAGE_NAMES[$index]}_seconds: ${STAGE_SECONDS[$index]}"
  done
  echo "total_seconds: ${TOTAL}"
} > "$TIMINGS_FILE"
echo
echo "Rehearsal PASSED — timings written to ${TIMINGS_FILE}:"
cat "$TIMINGS_FILE"
