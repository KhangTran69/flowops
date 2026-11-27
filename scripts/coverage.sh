#!/usr/bin/env bash
# Coverage gate: fails under the committed floor (ROADMAP Phase 3).
# Raise the floor as coverage improves; never lower it to make a PR pass.
# Locally: PYTHON=/path/to/venv/bin/python ./scripts/coverage.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python}"
[ -z "${PYTHON_OVERRIDE+x}" ] && [ -x ".venv/bin/python" ] && PYTHON=".venv/bin/python"
"$PYTHON" -m pytest -q --cov=src --cov-report=term-missing --cov-fail-under=83 "$@"
