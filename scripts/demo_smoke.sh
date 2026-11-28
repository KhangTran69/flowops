#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_URL="${FLOWOPS_DEMO_URL:-http://127.0.0.1:8000}"
KEY="portfolio-demo-9001"
TMP_DIR="$(mktemp -d)"
TENANT_A_PID=""
TENANT_B_PID=""
JOURNEY=0
for arg in "$@"; do
  case "$arg" in
    --journey) JOURNEY=1 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

cleanup() {
  local pid
  for pid in "$TENANT_A_PID" "$TENANT_B_PID"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
  rm -rf -- "$TMP_DIR"
}
trap cleanup EXIT

allocate_port() {
  python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

wait_ready() {
  local url="$1" pid="$2" label="$3" attempt
  for attempt in $(seq 1 50); do
    if curl --fail --silent "$url/ready" >/dev/null 2>&1; then
      return 0
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "${label} API exited before readiness." >&2
      return 1
    fi
    sleep 0.2
  done
  echo "Timed out waiting for ${label} API readiness." >&2
  return 1
}

# Auth headers for stacks that run with FLOWOPS_AUTH_REQUIRED=true. The local
# compose demo stack has auth off; the deployed demo takes FLOWOPS_DEMO_TOKEN.
AUTH=()
if [[ -n "${FLOWOPS_DEMO_TOKEN:-}" ]]; then
  AUTH=(-H "Authorization: Bearer ${FLOWOPS_DEMO_TOKEN}")
fi

api() {
  local method="$1" path="$2"; shift 2
  # ${AUTH[@]+...} guards the empty-array case on macOS bash 3.2 with set -u.
  curl --fail --silent --show-error -X "$method" ${AUTH[@]+"${AUTH[@]}"} "$@" "${BASE_URL}${path}"
}

# poll_for <description> <max-seconds> <command...> — retry a check every 2 s
# until it succeeds or the deadline passes. Workflow steps progress on the
# stack's own 2 s scheduler/worker/timer intervals.
poll_for() {
  local desc="$1" max="$2"; shift 2
  local deadline=$(( $(date +%s) + max ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if "$@" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "Timed out after ${max}s waiting for ${desc}." >&2
  return 1
}

echo "Checking ${BASE_URL}/health"
health="$(curl --fail --silent --show-error "${BASE_URL}/health")"
[[ "$health" == *'"status":"ok"'* ]]
echo "  ok: ${health}"

echo "Submitting and replaying an urgent network ticket"
payload='{"userId":7,"id":9001,"title":"VPN outage - all users cannot work","completed":false}'
curl --fail --silent --show-error \
  -D "${TMP_DIR}/first.headers" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY}" \
  -d "$payload" \
  "${BASE_URL}/tickets" > "${TMP_DIR}/first.json"
grep -q '"priority":"urgent"' "${TMP_DIR}/first.json"
grep -q '"queue":"it-network"' "${TMP_DIR}/first.json"
echo "  routing: urgent / it-network"

curl --fail --silent --show-error \
  -D "${TMP_DIR}/replay.headers" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY}" \
  -d "$payload" \
  "${BASE_URL}/tickets" > "${TMP_DIR}/replay.json"
grep -qi '^idempotency-replayed: true' "${TMP_DIR}/replay.headers"
echo "  replay: idempotency-replayed: true"

dashboard_status="$(curl --fail --silent --show-error -o /dev/null -w '%{http_code}' "${BASE_URL}/dashboard")"
[[ "$dashboard_status" == 200 ]]
echo "Dashboard check: HTTP ${dashboard_status}"

if [[ "$JOURNEY" == 0 ]]; then
  port_a="$(allocate_port)"
  port_b="$(allocate_port)"
  while [[ "$port_a" == "$port_b" ]]; do port_b="$(allocate_port)"; done
  token_a="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
  token_b="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
  db_url="sqlite:///${TMP_DIR}/tenant-demo.db"

  (
    cd "$ROOT"
    FLOWOPS_ENVIRONMENT=development FLOWOPS_AUTH_REQUIRED=true FLOWOPS_UI_AUTH_REQUIRED=false \
    FLOWOPS_DATABASE_URL="$db_url" FLOWOPS_DEV_BEARER_TOKEN="$token_a" \
    FLOWOPS_DEV_TENANT_KEY=tenant-a uvicorn src.api:app --host 127.0.0.1 --port "$port_a"
  ) >"${TMP_DIR}/tenant-a.log" 2>&1 &
  TENANT_A_PID=$!
  (
    cd "$ROOT"
    FLOWOPS_ENVIRONMENT=development FLOWOPS_AUTH_REQUIRED=true FLOWOPS_UI_AUTH_REQUIRED=false \
    FLOWOPS_DATABASE_URL="$db_url" FLOWOPS_DEV_BEARER_TOKEN="$token_b" \
    FLOWOPS_DEV_TENANT_KEY=tenant-b uvicorn src.api:app --host 127.0.0.1 --port "$port_b"
  ) >"${TMP_DIR}/tenant-b.log" 2>&1 &
  TENANT_B_PID=$!
  url_a="http://127.0.0.1:${port_a}"
  url_b="http://127.0.0.1:${port_b}"
  wait_ready "$url_a" "$TENANT_A_PID" "Tenant A"
  wait_ready "$url_b" "$TENANT_B_PID" "Tenant B"

  tenant_payload='{"userId":77,"id":91001,"title":"Tenant A demo ticket","completed":false}'
  curl --fail --silent --show-error -H 'Content-Type: application/json' \
    -H "Authorization: Bearer ${token_a}" -d "$tenant_payload" \
    "${url_a}/tickets" > "${TMP_DIR}/tenant-ticket.json"
  ticket_id="$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["ticket_id"])' "${TMP_DIR}/tenant-ticket.json")"
  curl --fail --silent --show-error -H "Authorization: Bearer ${token_a}" \
    "${url_a}/tickets/${ticket_id}" > "${TMP_DIR}/tenant-before.json"
  tenant_b_status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
    -H "Authorization: Bearer ${token_b}" "${url_b}/tickets/${ticket_id}")"
  [[ "$tenant_b_status" == 404 ]]
  curl --fail --silent --show-error -H "Authorization: Bearer ${token_a}" \
    "${url_a}/tickets/${ticket_id}" > "${TMP_DIR}/tenant-after.json"
  cmp -s "${TMP_DIR}/tenant-before.json" "${TMP_DIR}/tenant-after.json"
  echo "Tenant boundary: Tenant B HTTP ${tenant_b_status} for Tenant A ticket ${ticket_id}; Tenant A read unchanged."
  echo "FlowOps demo passed."
  exit 0
fi

# ---------------------------------------------------------------------------
# Guided journey (--journey): a day in the life of the FlowOps demo stack.
#
# Requires a FULL stack (app + worker + scheduler + timers + outbox), e.g.
#   docker compose up -d --wait
#   docker compose exec app python -m scripts.seed_demo
#   ./scripts/demo_smoke.sh --journey
# Every stop narrates what an operator would see; the matching prose lives
# in docs/portfolio-demo.md.

TS="$(date +%s)"
JOURNEY_REQUEST_ID=""
PAUSED_REQUEST_ID=""

find_request_id() {
  local employee="$1"
  api GET "/onboarding-requests" | python3 -c '
import json, sys
name = sys.argv[1]
for request in json.load(sys.stdin):
    if request.get("employee_name") == name:
        print(request["id"])
        break
else:
    sys.exit(1)
' "$employee"
}

request_item_field() {
  local request_id="$1" step_key="$2" field="$3"
  api GET "/onboarding-requests/${request_id}" | python3 -c '
import json, sys
for item in json.load(sys.stdin)["checklist"]:
    if item["step_key"] == sys.argv[2]:
        print(item[sys.argv[3]])
        break
' "$request_id" "$step_key" "$field"
}

journey_item_id() {
  local step_key="$1"
  api GET "/onboarding-requests/${JOURNEY_REQUEST_ID}" | python3 -c '
import json, sys
for item in json.load(sys.stdin)["checklist"]:
    if item["step_key"] == sys.argv[1]:
        print(item["id"])
        break
' "$step_key"
}

journey_step_completed() {
  [[ "$(request_item_field "$JOURNEY_REQUEST_ID" "$1" status)" == "completed" ]]
}

paused_step_requeued() {
  [[ "$(request_item_field "$PAUSED_REQUEST_ID" create_user_account queue)" == "it-management" ]]
}

overdue_log_present() {
  api GET "/execution-logs?limit=200" | grep -q 'onboarding.step.overdue'
}

stop_trigger() {
  local secret signature body webhook_ts
  echo
  echo "== Stop 2 of 6: an event fires a trigger =="
  echo "   The seeded HR webhook source rotates its secret (the API shows a"
  echo "   secret exactly once), the journey signs a new-hire event with it,"
  echo "   and the durable job queue turns the delivery into an onboarding"
  echo "   request pinned to the onboarding-dag definition."
  secret="$(api POST "/event-sources/demo-source/rotate-secret" | python3 -c 'import json,sys; print(json.load(sys.stdin)["secret"])')"
  body="$(python3 - "$TS" <<'PY'
import json, sys
print(json.dumps({
    "event_id": f"journey-{sys.argv[1]}",
    "name": f"Journey Newhire {sys.argv[1]}",
    "email": f"journey.newhire.{sys.argv[1]}@example.test",
    "department": "Support",
    "requester_id": 9,
}))
PY
)"
  webhook_ts="$(date +%s)"
  signature="$(python3 - "$secret" "$webhook_ts" "$body" <<'PY'
import hashlib, hmac, sys
secret, ts, body = sys.argv[1], sys.argv[2], sys.argv[3]
print("sha256=" + hmac.new(secret.encode(), f"{ts}.".encode() + body.encode(), hashlib.sha256).hexdigest())
PY
)"
  api POST "/webhooks/triggers/demo-new-hire" \
    -H 'Content-Type: application/json' \
    -H "X-FlowOps-Timestamp: ${webhook_ts}" \
    -H "X-FlowOps-Signature: ${signature}" \
    -d "$body" | grep -q '"status":"queued"'
  echo "   delivery accepted and queued (event journey-${TS})"
  poll_for "the triggered onboarding request" 60 \
    find_request_id "Journey Newhire ${TS}"
  echo "   worker processed the delivery -> onboarding request created"
}

stop_workflow_run() {
  echo
  echo "== Stop 3 of 6: a workflow run takes over =="
  echo "   A request created through the API starts a versioned DAG run; the"
  echo "   scheduler resolves the graph and enqueues every ready step as a"
  echo "   durable, leased job."
  api POST "/onboarding-requests" \
    -H 'Content-Type: application/json' \
    -d "{\"request_type\":\"onboarding\",\"employee_name\":\"Journey Run ${TS}\",\"employee_email\":\"journey.run.${TS}@example.test\",\"department\":\"Support\",\"requester_id\":9,\"start_date\":\"2026-12-14\",\"template\":\"onboarding-dag\"}" \
    > "${TMP_DIR}/journey-request.json"
  JOURNEY_REQUEST_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "${TMP_DIR}/journey-request.json")"
  api POST "/onboarding-requests/${JOURNEY_REQUEST_ID}/run" >/dev/null
  echo "   DAG run started for request ${JOURNEY_REQUEST_ID}"
  poll_for "run steps to appear (scheduler tick)" 60 \
    api GET "/onboarding-requests/${JOURNEY_REQUEST_ID}/run"
  echo "   scheduler is orchestrating the run"
}

stop_approval() {
  echo
  echo "== Stop 4 of 6: the human approval gate holds =="
  echo "   Approval-required steps stay pending until an operator approves"
  echo "   them; a step whose prerequisites are still blocked refuses early"
  echo "   approval (409), so the journey approves in DAG order."
  local item_id
  item_id="$(journey_item_id create_user_account)"
  api POST "/onboarding-requests/${JOURNEY_REQUEST_ID}/items/${item_id}/approve" >/dev/null
  echo "   approved 'Create user account'"
  poll_for "step create_user_account to complete" 90 journey_step_completed create_user_account
  item_id="$(journey_item_id grant_licenses)"
  api POST "/onboarding-requests/${JOURNEY_REQUEST_ID}/items/${item_id}/approve" >/dev/null
  echo "   approved 'Grant software licenses' (unblocked by the first completion)"
  poll_for "step grant_licenses to complete" 90 journey_step_completed grant_licenses
  echo "   both gated steps completed only after approval"
}

stop_sla() {
  echo
  echo "== Stop 5 of 6: an SLA timer escalates =="
  echo "   The seeded paused run's first step breached its SLA about 90 s"
  echo "   after seeding; the timers service logged the breach, re-queued the"
  echo "   step to the escalation queue, and wrote an escalation notification."
  poll_for "the overdue escalation log entry" 180 overdue_log_present
  echo "   execution log records onboarding.step.overdue"
  PAUSED_REQUEST_ID="$(find_request_id "Marcus Webb")"
  poll_for "the breached step to move to it-management" 30 paused_step_requeued
  echo "   step re-queued to the escalation queue (it-management)"
  echo "   request ${PAUSED_REQUEST_ID} itself is still paused awaiting approval"
}

stop_analytics() {
  echo
  echo "== Stop 6 of 6: analytics reflects the day =="
  local status
  status="$(curl --fail --silent --show-error -o /dev/null -w '%{http_code}' "${BASE_URL}/ui/analytics")"
  [[ "$status" == 200 ]]
  echo "   /ui/analytics renders runs by status, approval counts, SLA timers,"
  echo "   tenants, and step bottlenecks (HTTP ${status})"
}

echo
echo "== Stop 1 of 6: a ticket arrives =="
echo "   (shown above) intake validated the payload, classified it as"
echo "   network/urgent, routed it to it-network, and persisted an"
echo "   execution log; the replayed request was served idempotently."

stop_trigger
stop_workflow_run
stop_approval
stop_sla
stop_analytics

echo
echo "FlowOps guided journey passed."
