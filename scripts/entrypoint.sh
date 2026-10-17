#!/bin/sh
set -eu

if [ "${FLOWOPS_RUN_MIGRATIONS:-true}" = "true" ]; then
  echo "Running database migrations..."
  alembic upgrade head
fi

exec "$@"
