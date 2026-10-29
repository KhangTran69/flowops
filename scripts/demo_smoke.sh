#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_URL="${FLOWOPS_DEMO_URL:-http://127.0.0.1:8000}"
KEY="portfolio-demo-9001"
TMP_DIR="$(mktemp -d)"
TENANT_A_PID=""
TENANT_B_PID=""

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

echo "Checking ${BASE_URL}/health"
health="$(curl --fail --silent --show-error "${BASE_URL}/health")"
[[ "$health" == *'"status":"ok"'* ]]
echo "  ok: ${health}"

payload='{"userId":7,"id":9001,"title":"VPN outage - all users cannot work","completed":false}'
echo "Submitting and replaying an urgent network ticket"
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
