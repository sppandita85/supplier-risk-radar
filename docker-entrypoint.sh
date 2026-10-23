#!/bin/sh
# Wait for Postgres, apply the schema (idempotent), then run the given command.
set -e
for i in $(seq 1 30); do
  python -m radar.db >/dev/null 2>&1 && break
  echo "waiting for postgres ($i)..."; sleep 2
done
python -m radar.db
exec "$@"
