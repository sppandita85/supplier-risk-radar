# Supplier Risk Radar

Monitors 5 suppliers. Updates arrive as files, a local LLM separates **signals** from **noise**,
a correlation job turns series of signals into **alerts** (or discards them), and a Streamlit
dashboard shows both the insights and how the radar itself is operating.

```
data/input/<SUPPLIER>/*.json|txt
        │
        ▼
radar.ingest ──► Ollama llama3.1:8b ──► classifications (signal + noise, with reasoning, latency, tokens)
        │                                     │
        ▼                                     ▼
data/processed | data/failed           radar.correlate ──► alert_candidates (alert | discarded + reason)
                                              │
                                              ▼
                                           alerts ──► dashboard (Operations + 4 insight tabs)
```

## Suppliers
`APEX` Apex Semiconductors · `NORDIC` Nordic Steelworks · `MERIDIAN` Meridian Logistics ·
`VERDANT` Verdant Chemicals · `SUNRISE` Sunrise Textiles — see [config/suppliers.yaml](config/suppliers.yaml).

## Run with Docker
Image: `ghcr.io/sppandita85/supplier-risk-radar:latest` (linux/amd64 + linux/arm64).
Needs [Ollama](https://ollama.com) on the host with `ollama pull llama3.1:8b`.

```bash
docker compose up -d                                   # Postgres + dashboard (http://localhost:8502) + watcher
docker compose run --rm worker python -m radar.synth --days 60   # drop synthetic updates into ./data/input
# the watcher classifies them and runs both correlation jobs; refresh the dashboard
docker compose --profile phoenix up -d                 # optional: bundled Phoenix on :6006
```
By default, traces go to a Phoenix already running on the host at `:6006`. Override any setting with
environment variables (`OLLAMA_URL`, `OLLAMA_MODEL`, `PHOENIX_COLLECTOR_ENDPOINT`, `DASHBOARD_PORT`, `TRACING`).

## Local setup
```bash
pg_ctl -D /usr/local/var/postgresql@14 start     # or however you run Postgres locally
ollama pull llama3.1:8b                          # already present on this machine
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./scripts/init_db.sh
```

## Run
```bash
./scripts/run_pipeline.sh                  # synth (60 days) -> ingest -> correlate  (~10 min LLM time)
.venv/bin/streamlit run dashboard/app.py   # http://localhost:8501
```
Step by step: `python -m radar.synth`, `python -m radar.ingest [--limit N]`, `python -m radar.correlate`.

Live demo: in one terminal `python -m radar.watch`, in another `python -m radar.synth --live 10 --interval 15`.
New files get classified and correlated as they land; hit **Refresh** in the dashboard.

Reset everything: `./scripts/reset.sh`.

## Input format
JSON: `{"supplier_id", "source_type": news|email|erp|rating|social, "published_at", "title", "body"}`.
Plain `.txt` also works: first line = title, rest = body; supplier comes from the folder name.

## How signals are decided
- LLM returns `is_signal, category, severity 1-5, sentiment, confidence, reasoning` (JSON mode, temperature 0).
- Kept as signal only if the LLM says signal **and** confidence ≥ 0.5; otherwise stored as noise with
  `discard_reason` (`llm_noise`, `low_confidence`, `llm_error`). Duplicate content is skipped by hash.

## How alerts are decided ([config/rules.yaml](config/rules.yaml))
Signals for a supplier are chained into episodes (gaps ≤ 10 days). Each rule scores the episode
(Σ severity × confidence, weighted by supplier criticality):

| Rule | Fires when |
|---|---|
| burst | ≥ 3 signals within 7 days |
| severity_accumulation | ≥ 2 signals in 14 days with enough combined weight |
| multi_category | ≥ 2 different risk categories within 10 days |
| escalation | ≥ 3 consecutive signals with rising severity |
| critical_event | any severity-5 signal with confidence ≥ 0.8 |

### LLM correlation ([radar/correlate_llm.py](radar/correlate_llm.py))
Runs alongside the rules. For each supplier, the local LLM reads **all signals from the last 7 days
together** and decides whether they form an actionable pattern. It returns severity, confidence,
a title, a summary, a recommended action and its reasoning. Alerts need confidence ≥ 0.6 and
severity above low. If the previous week already has an open LLM alert for that supplier, the model
sees it and the alert is extended instead of duplicated.

```bash
python -m radar.correlate_llm                     # the 7 days ending now
python -m radar.correlate_llm --as-of 2026-10-05  # the 7 days ending on a date
python -m radar.correlate_llm --backfill          # every week back to the first signal (for history)
```
Its decisions are stored in the same tables with `method = 'llm'`, so the dashboard can compare
rules vs LLM. `python -m radar.watch --correlation rules|llm|both` picks which run on new files.

### Rule-based correlation
Best rule with score ≥ `alert_threshold` raises the alert. Everything else is stored in
`alert_candidates` as `discarded` with a reason (single signal, below threshold, rule not met,
merged into another alert). Re-running is idempotent; growing episodes update their open alert.

## Ask the radar (chat)
The dashboard's **💬 Ask the radar** tab lets business users ask questions such as
"Which suppliers have critical alerts right now?" or "Why was Verdant not flagged?".
The question goes to the local LLM ([radar/chat.py](radar/chat.py)) with 5 read-only tools:
`list_alerts`, `get_alert_details`, `get_supplier_signals`, `get_risk_overview`,
`get_discarded_decisions`. The LLM decides which to call (up to 4 rounds), gets the rows from
Postgres, and writes the answer. It never writes SQL itself. Each answer shows the tool calls
behind it, and every question is logged to `chat_log` (shown on the Operations tab).
CLI: `python -m radar.chat "Tell me about alert 9"`.

## Observability (Phoenix)
Every LLM step is traced to Arize Phoenix (Docker container on http://localhost:6006, project
`supplier-risk-radar`) through OpenTelemetry with OpenInference span kinds ([radar/tracing.py](radar/tracing.py)):

| Root span | Kind | Children |
|---|---|---|
| `ingest.update` (one per file) | CHAIN | `llm.classify_update` (LLM: prompt, JSON reply, tokens) |
| `correlate_llm.review_week` (one per supplier-week) | CHAIN | `llm.weekly_review` (LLM) |
| `correlate_rules.run` | CHAIN | run statistics |
| `chat.ask` (grouped by `session.id`) | AGENT | `llm.chat` (LLM, with tool schemas) → `tool.<name>` (TOOL) → `llm.chat` |

Trace ids are stored on `classifications`, `alert_candidates` and `chat_log`, so you can go from a
Postgres row to its trace. The dashboard sidebar links to the project, and each chat answer
links to its trace. Configure with `PHOENIX_COLLECTOR_ENDPOINT`, `PHOENIX_PROJECT`, `TRACING=off`.

## Tables
`suppliers`, `ingestion_runs`, `raw_updates`, `classifications`, `ingestion_failures`,
`signals` (view), `correlation_runs`, `alert_candidates`, `alerts`, `chat_log` — see [db/schema.sql](db/schema.sql).
