-- Supplier Risk Radar schema (idempotent)

CREATE TABLE IF NOT EXISTS suppliers (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    category      TEXT,
    country       TEXT,
    tier          INT,
    criticality   INT
);

-- One row per ingestion job run
CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id        SERIAL PRIMARY KEY,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    files_seen    INT DEFAULT 0,
    files_ok      INT DEFAULT 0,
    files_failed  INT DEFAULT 0,
    duplicates    INT DEFAULT 0,
    signals       INT DEFAULT 0,
    noise         INT DEFAULT 0,
    model         TEXT,
    status        TEXT DEFAULT 'running'      -- running | ok | partial | failed
);

-- Every update file received (signal or noise)
CREATE TABLE IF NOT EXISTS raw_updates (
    id            SERIAL PRIMARY KEY,
    run_id        INT REFERENCES ingestion_runs(run_id),
    supplier_id   TEXT REFERENCES suppliers(id),
    source_file   TEXT NOT NULL,
    source_type   TEXT,
    published_at  TIMESTAMPTZ,
    title         TEXT,
    body          TEXT,
    file_hash     TEXT UNIQUE,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- LLM decision for every update: kept (signal) AND discarded (noise)
CREATE TABLE IF NOT EXISTS classifications (
    id             SERIAL PRIMARY KEY,
    update_id      INT UNIQUE REFERENCES raw_updates(id) ON DELETE CASCADE,
    is_signal      BOOLEAN NOT NULL,
    llm_is_signal  BOOLEAN,               -- raw LLM verdict before post-rules
    category       TEXT,
    severity       INT,
    sentiment      REAL,
    confidence     REAL,
    reasoning      TEXT,
    discard_reason TEXT,                  -- why it was treated as noise
    model          TEXT,
    prompt_version TEXT,
    latency_ms     INT,
    prompt_tokens  INT,
    output_tokens  INT,
    attempts       INT DEFAULT 1,
    error          TEXT,
    classified_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Files that could not be processed at all
CREATE TABLE IF NOT EXISTS ingestion_failures (
    id           SERIAL PRIMARY KEY,
    run_id       INT REFERENCES ingestion_runs(run_id),
    source_file  TEXT,
    supplier_id  TEXT,
    stage        TEXT,                    -- parse | llm | db
    error        TEXT,
    failed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE VIEW signals AS
SELECT c.id AS classification_id, u.id AS update_id, u.supplier_id, s.name AS supplier_name,
       u.source_type, u.published_at, u.title, u.body,
       c.category, c.severity, c.sentiment, c.confidence, c.reasoning, c.classified_at
FROM classifications c
JOIN raw_updates u ON u.id = c.update_id
JOIN suppliers s   ON s.id = u.supplier_id
WHERE c.is_signal;

CREATE TABLE IF NOT EXISTS correlation_runs (
    run_id         SERIAL PRIMARY KEY,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    signals_scanned INT DEFAULT 0,
    candidates     INT DEFAULT 0,
    alerts_raised  INT DEFAULT 0,
    discarded      INT DEFAULT 0,
    rules_version  TEXT,
    status         TEXT DEFAULT 'running'
);

-- Every pattern the correlation job evaluated: promoted to alert OR discarded
CREATE TABLE IF NOT EXISTS alert_candidates (
    id             SERIAL PRIMARY KEY,
    run_id         INT REFERENCES correlation_runs(run_id),
    supplier_id    TEXT REFERENCES suppliers(id),
    rule_id        TEXT NOT NULL,
    window_start   TIMESTAMPTZ,
    window_end     TIMESTAMPTZ,
    signal_ids     INT[],
    score          REAL,
    decision       TEXT NOT NULL,          -- alert | discarded
    discard_reason TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS alerts (
    id            SERIAL PRIMARY KEY,
    candidate_id  INT REFERENCES alert_candidates(id),
    supplier_id   TEXT REFERENCES suppliers(id),
    rule_id       TEXT,
    title         TEXT,
    severity      TEXT,                    -- low | medium | high | critical
    score         REAL,
    summary       TEXT,
    signal_ids    INT[],
    window_start  TIMESTAMPTZ,
    window_end    TIMESTAMPTZ,
    status        TEXT NOT NULL DEFAULT 'open',  -- open | acknowledged | closed
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- LLM-based correlation (radar.correlate_llm) shares these tables; `method` tells them apart
ALTER TABLE correlation_runs ADD COLUMN IF NOT EXISTS method TEXT DEFAULT 'rules';
ALTER TABLE correlation_runs ADD COLUMN IF NOT EXISTS model TEXT;
ALTER TABLE alert_candidates ADD COLUMN IF NOT EXISTS method TEXT DEFAULT 'rules';
ALTER TABLE alert_candidates ADD COLUMN IF NOT EXISTS llm_confidence REAL;
ALTER TABLE alert_candidates ADD COLUMN IF NOT EXISTS llm_reasoning TEXT;
ALTER TABLE alert_candidates ADD COLUMN IF NOT EXISTS latency_ms INT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS method TEXT DEFAULT 'rules';
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS recommended_action TEXT;

-- Business-user chat: every question, the tools the LLM called, and the answer
CREATE TABLE IF NOT EXISTS chat_log (
    id          SERIAL PRIMARY KEY,
    session_id  TEXT,
    question    TEXT NOT NULL,
    answer      TEXT,
    tools_used  TEXT[],
    tool_calls  JSONB,
    latency_ms  INT,
    model       TEXT,
    error       TEXT,
    asked_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Phoenix trace ids, to jump from a Postgres row to its trace
ALTER TABLE classifications ADD COLUMN IF NOT EXISTS trace_id TEXT;
ALTER TABLE alert_candidates ADD COLUMN IF NOT EXISTS trace_id TEXT;
ALTER TABLE chat_log ADD COLUMN IF NOT EXISTS trace_id TEXT;

CREATE INDEX IF NOT EXISTS ix_updates_supplier_pub ON raw_updates(supplier_id, published_at);
CREATE INDEX IF NOT EXISTS ix_class_signal ON classifications(is_signal);
CREATE INDEX IF NOT EXISTS ix_cand_supplier ON alert_candidates(supplier_id, rule_id);
CREATE INDEX IF NOT EXISTS ix_alerts_supplier ON alerts(supplier_id, status);
