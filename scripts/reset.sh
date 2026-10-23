#!/usr/bin/env bash
# Wipe all radar data and files (keeps the schema).
set -euo pipefail
cd "$(dirname "$0")/.."
psql risk_radar -c "TRUNCATE alerts, alert_candidates, correlation_runs, classifications, ingestion_failures, raw_updates, ingestion_runs RESTART IDENTITY CASCADE"
rm -rf data/input/* data/processed/* data/failed/*
