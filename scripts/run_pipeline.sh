#!/usr/bin/env bash
# Full demo: generate synthetic data -> LLM classification -> correlation.
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python -m radar.synth --days "${DAYS:-60}" --seed "${SEED:-42}"
.venv/bin/python -m radar.ingest
.venv/bin/python -m radar.correlate
.venv/bin/python -m radar.correlate_llm --backfill
