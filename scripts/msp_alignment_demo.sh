#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${FLOWOPS_API_URL:-http://127.0.0.1:8000}"

echo '== readiness =='
curl -fsS "$BASE_URL/ready"
printf '\n== tickets ==\n'
curl -fsS "$BASE_URL/tickets?limit=2"
printf '\n== workflow analytics ==\n'
curl -fsS "$BASE_URL/analytics/summary?days=7"
printf '\n== reporting summary ==\n'
curl -fsS "$BASE_URL/reports/summary?days=7"
printf '\n== tenant drift check ==\n'
curl -fsS -X POST "$BASE_URL/tenants/drift-check"
printf '\n== metrics (first 40 lines) ==\n'
curl -fsS "$BASE_URL/metrics" | sed -n '1,40p'
printf '\nDemo checks completed.\n'
