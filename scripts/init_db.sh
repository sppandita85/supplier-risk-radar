#!/usr/bin/env bash
# Create the risk_radar database (if missing) and apply the schema.
set -euo pipefail
cd "$(dirname "$0")/.."
createdb risk_radar 2>/dev/null || true
.venv/bin/python -m radar.db
