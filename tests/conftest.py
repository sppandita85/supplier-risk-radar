"""Shared fixtures.

Unit tests need nothing external. Integration tests (marked `integration`) need a Postgres
reachable through PG_DSN; they wipe and recreate the schema, so never point PG_DSN at real data.
The LLM is always replaced by a deterministic stub, so CI does not need Ollama.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("TRACING", "off")

SIGNAL_WORDS = {  # keyword -> (category, severity) used by the stub classifier
    "fire": ("operational", 5), "force majeure": ("operational", 5), "credit watch": ("financial", 4),
    "layoffs": ("operational", 3), "delayed 14": ("logistics", 3), "delayed 8": ("logistics", 3),
    "strike": ("logistics", 4), "diversions": ("logistics", 3), "at risk": ("logistics", 3),
    "fails third-party": ("esg", 4), "wage protests": ("esg", 4), "suspends orders": ("financial", 4),
    "labelling discrepancy": ("compliance", 2), "surcharge": ("financial", 2),
    "delayed 3": ("logistics", 2), "delayed 2": ("logistics", 2), "price adjustment": ("financial", 2),
    "quality deviation": ("quality", 2), "vote on strike": ("logistics", 3),
}


def fake_classify(supplier, update, retries=1):
    text = f"{update.get('title', '')} {update.get('body', '')}".lower()
    hit = next(((c, s) for k, (c, s) in SIGNAL_WORDS.items() if k in text), None)
    base = dict(latency_ms=5, prompt_tokens=100, output_tokens=40, attempts=1, model="stub")
    if hit:
        return {**base, "is_signal": True, "category": hit[0], "severity": hit[1], "sentiment": -0.6,
                "confidence": 0.9, "reasoning": "stub: risk keyword"}
    return {**base, "is_signal": False, "category": "none", "severity": 1, "sentiment": 0.3,
            "confidence": 0.9, "reasoning": "stub: routine"}


def sig(uid, days_ago, severity, category="operational", confidence=0.9, title=None):
    """A signal row shaped like the `signals` view."""
    return {"update_id": uid, "published_at": datetime(2026, 10, 1, tzinfo=timezone.utc) - timedelta(days=days_ago),
            "severity": severity, "category": category, "confidence": confidence,
            "title": title or f"signal {uid}", "body": "", "source_type": "news"}


@pytest.fixture
def db(monkeypatch, tmp_path):
    """Fresh schema in the PG_DSN database, plus isolated data folders."""
    if "PG_DSN" not in os.environ:
        pytest.skip("PG_DSN not set; integration tests need Postgres")
    from radar import config, ingest, synth
    from radar import db as dbmod
    with dbmod.connect() as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")
    dbmod.init_schema()
    for mod in (config, ingest, synth):
        for name, sub in (("INPUT_DIR", "input"), ("PROCESSED_DIR", "processed"), ("FAILED_DIR", "failed")):
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, tmp_path / sub)
    (tmp_path / "input").mkdir()
    monkeypatch.setattr("radar.llm.classify", fake_classify)
    return tmp_path
