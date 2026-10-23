"""End-to-end pipeline tests against a real Postgres (LLM stubbed). Run with PG_DSN set."""
import json

import pytest

from radar import chat, correlate, correlate_llm, ingest, synth
from radar.db import connect

pytestmark = pytest.mark.integration


def _scalar(sql, *args):
    with connect() as conn:
        return list(conn.execute(sql, args).fetchone().values())[0]


@pytest.fixture
def ingested(db):
    synth.batch(days=60, seed=42)
    stats = ingest.run(verbose=False)
    return db, stats


def test_ingest_stores_signals_noise_and_failures(ingested):
    tmp, stats = ingested
    assert stats["signals"] > 5 and stats["noise"] > 50
    assert stats["duplicates"] == 1 and stats["failed"] == 1          # the seeded dup + corrupt file
    assert _scalar("SELECT count(*) FROM classifications") == stats["ok"]
    assert _scalar("SELECT count(*) FROM ingestion_failures WHERE stage='parse'") == 1
    assert not list((tmp / "input").glob("*/*.json"))                 # everything moved out of input/
    assert list((tmp / "failed").glob("*/*"))


def test_reingest_is_idempotent(ingested):
    before = _scalar("SELECT count(*) FROM raw_updates")
    assert ingest.run(verbose=False)["ok"] == 0
    assert _scalar("SELECT count(*) FROM raw_updates") == before


def test_rule_correlation_flags_storylines(ingested):
    correlate.run(verbose=False)
    alerted = {r for (r,) in _rows("SELECT DISTINCT supplier_id FROM alerts WHERE method='rules'")}
    assert {"APEX", "MERIDIAN"} <= alerted
    assert "VERDANT" not in alerted                                   # single minor issue -> discarded
    assert _scalar("SELECT count(*) FROM alert_candidates WHERE decision='discarded'") > 0
    n = _scalar("SELECT count(*) FROM alerts")
    correlate.run(verbose=False)                                      # rerun creates nothing new
    assert _scalar("SELECT count(*) FROM alerts") == n


def test_llm_correlation_alerts_and_extends(ingested, monkeypatch):
    def fake_review(system, prompt, validate, **kw):
        severe = any(f"severity {s}/5" in prompt for s in (4, 5))
        return validate({"raise_alert": severe, "severity": "critical" if severe else "low", "confidence": 0.9,
                         "title": "stub alert", "summary": "stub", "recommended_action": "call supplier",
                         "reasoning": "stub"}) | {"latency_ms": 7}
    monkeypatch.setattr("radar.llm.generate_json", fake_review)
    stats = correlate_llm.run(backfill=True, verbose=False)
    assert stats["alerts"] >= 1 and stats["discarded"] >= 1
    assert _scalar("SELECT count(*) FROM alerts WHERE method='llm' AND supplier_id='APEX'") == 1
    assert _scalar("SELECT count(*) FROM alert_candidates WHERE method='llm' AND supplier_id='VERDANT' "
                   "AND decision <> 'discarded'") == 0


def test_chat_tools_read_alerts(ingested):
    correlate.run(verbose=False)
    alerts = json.loads(chat.run_tool("list_alerts", {"supplier": "apex"}))
    assert alerts and all(a["supplier"] == "Apex Semiconductors" for a in alerts)
    details = json.loads(chat.run_tool("get_alert_details", {"alert_id": alerts[0]["id"]}))
    assert details["contributing_signals"]
    assert "error" in json.loads(chat.run_tool("get_supplier_signals", {"supplier": "nobody"}))
    assert "error" in json.loads(chat.run_tool("drop_tables", {}))


def test_chat_agent_loop_calls_tool_then_answers(ingested, monkeypatch):
    replies = iter([
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "get_risk_overview", "arguments": {}}}]},
        {"role": "assistant", "content": "Apex is the riskiest supplier."},
    ])
    monkeypatch.setattr(chat, "_chat", lambda messages, tools=True: next(replies))
    out = chat.ask("Which supplier is riskiest?", session_id="ci")
    assert out["answer"] == "Apex is the riskiest supplier."
    assert [t["name"] for t in out["tool_calls"]] == ["get_risk_overview"]
    assert _scalar("SELECT count(*) FROM chat_log WHERE session_id='ci'") == 1


def test_dashboard_renders_all_tabs(ingested):
    from streamlit.testing.v1 import AppTest
    correlate.run(verbose=False)
    at = AppTest.from_file("../dashboard/app.py", default_timeout=60).run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(at.tabs) == 6


def _rows(sql):
    with connect() as conn:
        return [tuple(r.values()) for r in conn.execute(sql).fetchall()]
