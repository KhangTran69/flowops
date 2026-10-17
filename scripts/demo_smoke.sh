#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${FLOWOPS_DEMO_URL:-http://127.0.0.1:8000}"
KEY="portfolio-demo-9001"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

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
echo "  created: $(cat "${TMP_DIR}/first.json")"

curl --fail --silent --show-error \
  -D "${TMP_DIR}/replay.headers" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: ${KEY}" \
  -d "$payload" \
  "${BASE_URL}/tickets" > "${TMP_DIR}/replay.json"

grep -qi '^idempotency-replayed: true' "${TMP_DIR}/replay.headers"
echo "  replayed: $(cat "${TMP_DIR}/replay.json")"

dashboard_status="$(curl --fail --silent --show-error -o /dev/null -w '%{http_code}' "${BASE_URL}/dashboard")"
[[ "$dashboard_status" == 200 ]]
echo "Dashboard check: HTTP ${dashboard_status}"
echo "FlowOps demo passed."
