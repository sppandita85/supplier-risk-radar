"""LLM correlation job: for each supplier, the local LLM reviews the last 7 days of signals
together and decides whether they add up to an alert.

    python -m radar.correlate_llm                     # window = the 7 days ending now
    python -m radar.correlate_llm --as-of 2026-10-05  # window = the 7 days ending on that date
    python -m radar.correlate_llm --backfill          # step week by week over all history
    python -m radar.correlate_llm --suppliers APEX VERDANT --days 7

Every review is stored in alert_candidates (method='llm'), alert or discarded, with the LLM's
reasoning. Re-running is safe: a supplier whose window holds exactly the same signals as an
earlier review is skipped, and a window that overlaps an open LLM alert updates that alert.
"""
import argparse
from datetime import datetime, timedelta, timezone

from . import llm, tracing
from .config import OLLAMA_MODEL, load_rules
from .db import connect, init_schema

PROMPT_VERSION = "corr-v1"
MIN_ALERT_CONFIDENCE = 0.6
SEVERITIES = ["low", "medium", "high", "critical"]

SYSTEM = """You are a senior supply-chain risk analyst. You receive every risk signal recorded
for ONE supplier during a recent time window (signals were already filtered from noise).
Look at them TOGETHER and decide whether they form a pattern that the buyer must act on now.

Raise an alert when signals corroborate or compound each other, for example:
- one event with knock-on effects (incident -> delays -> force majeure -> financial stress)
- several independent problems at once (financial + operational + compliance)
- a worsening trend in severity or frequency
- a single event so severe that supply is clearly at risk (plant shutdown, insolvency, sanctions)
Do NOT raise an alert for isolated, minor, routine or already-resolved issues. These are routine:
- a price increase below 10%, an energy or fuel surcharge
- one shipment delayed by less than 7 days
- a minor audit or labelling finding with corrective action filed
A single signal is only an alert if it alone threatens supply (e.g. plant fire, shutdown,
insolvency, sanctions, force majeure). Supplier criticality tells you how much an alert matters,
NOT whether a pattern exists: never raise an alert just because the supplier is critical.

Worked examples:
- 1 signal "Supplier announces 4% price increase" -> raise_alert false (routine price change).
- 1 signal "Shipment delayed 3 days" -> raise_alert false (routine delay).
- "Plant fire" + "PO delayed 14 days" + "force majeure" -> raise_alert true, critical.
- "Failed social audit" + "wage protests close factory" -> raise_alert true, high.

If an OPEN ALERT from the previous window is shown and this window's signals continue or
worsen the same situation, set raise_alert=true so the open alert is extended and updated.
If they are unrelated routine issues, set raise_alert=false.

Respond ONLY with JSON of this exact shape:
{"raise_alert": true|false,
 "severity": "low"|"medium"|"high"|"critical",
 "confidence": number 0-1,
 "title": "short alert headline (max 12 words)",
 "summary": "2-3 sentences: what is happening and why it matters to the buyer",
 "key_signal_ids": [ids of the signals that drive the decision],
 "risk_themes": ["financial"|"operational"|"geopolitical"|"compliance"|"esg"|"cyber"|"logistics"|"quality"],
 "recommended_action": "one concrete next step for the buyer",
 "reasoning": "one sentence explaining the decision"}
"""


def _validate(d: dict) -> dict:
    ra = d.get("raise_alert")
    if not isinstance(ra, bool):
        if str(ra).lower() not in ("true", "false"):
            raise llm.LLMError(f"bad raise_alert: {ra!r}")
        d["raise_alert"] = str(ra).lower() == "true"
    sev = str(d.get("severity", "low")).lower()
    d["severity"] = sev if sev in SEVERITIES else "medium"
    d["confidence"] = max(0.0, min(1.0, float(d.get("confidence", 0.5))))
    ids = d.get("key_signal_ids") or []
    d["key_signal_ids"] = [int(i) for i in ids if str(i).strip().lstrip("#").isdigit()] if isinstance(ids, list) else []
    for k, n in (("title", 150), ("summary", 1000), ("recommended_action", 400), ("reasoning", 500)):
        d[k] = str(d.get(k, ""))[:n]
    d["risk_themes"] = [str(t) for t in d.get("risk_themes", [])][:8] if isinstance(d.get("risk_themes"), list) else []
    return d


def _prompt(sup: dict, sigs: list[dict], start: datetime, end: datetime, open_alert: dict | None) -> str:
    lines = [f"Supplier: {sup['name']} ({sup['category']}, {sup['country']}, tier {sup['tier']}, "
             f"criticality {sup['criticality']}/5 to the buyer)",
             f"Window: {start:%Y-%m-%d} to {end:%Y-%m-%d} ({len(sigs)} signals)", ""]
    if open_alert:
        lines += [f"OPEN ALERT from previous window ({open_alert['severity']}): {open_alert['title']}. "
                  f"{open_alert['summary']}", ""]
    lines.append("Signals:")
    for s in sigs:
        lines.append(f"- id {s['update_id']} | {s['published_at']:%Y-%m-%d} | {s['source_type']} | "
                     f"{s['category']} | severity {s['severity']}/5 | confidence {s['confidence']:.2f}\n"
                     f"  {s['title']}: {(s['body'] or '')[:300]}")
    return "\n".join(lines)


def review_window(conn, run_id: int, sup: dict, start: datetime, end: datetime, stats: dict,
                  verbose: bool) -> dict | None:
    min_conf = load_rules().get("min_confidence", 0.5)
    sigs = conn.execute(
        "SELECT * FROM signals WHERE supplier_id=%s AND confidence >= %s AND published_at > %s "
        "AND published_at <= %s ORDER BY published_at", (sup["id"], min_conf, start, end)).fetchall()
    if not sigs:
        return None
    stats["scanned"] += len(sigs)
    ids = sorted(s["update_id"] for s in sigs)
    if conn.execute("SELECT 1 FROM alert_candidates WHERE method='llm' AND supplier_id=%s AND "
                    "signal_ids @> %s::int[] AND signal_ids <@ %s::int[] LIMIT 1",
                    (sup["id"], ids, ids)).fetchone():
        stats["skipped"] += 1
        return None

    with tracing.span("correlate_llm.review_week", kind="CHAIN",
                      input={"supplier": sup["id"], "window": f"{start:%Y-%m-%d} to {end:%Y-%m-%d}",
                             "signals": [f"#{s['update_id']} {s['published_at']:%Y-%m-%d} sev{s['severity']} "
                                         f"{s['title']}" for s in sigs]},
                      run_id=run_id, supplier=sup["id"], window_end=f"{end:%Y-%m-%d}", signals=len(sigs)) as sp:
        out = _decide(conn, run_id, sup, start, end, sigs, ids, stats, tracing.trace_id(sp))
        tracing.set_output(sp, out, ok=out["decision"] != "error")
        sp.set_attribute("radar.decision", out["decision"])
    if verbose:
        if out["decision"] in ("alert", "updated"):
            print(f"{sup['id']:<9} {end:%Y-%m-%d} ALERT #{out['alert_id']} {out['severity']} ({len(sigs)} signals, "
                  f"conf {out['confidence']:.2f}, {out['latency_ms']}ms): {out['title']}")
        else:
            print(f"{sup['id']:<9} {end:%Y-%m-%d} {out['decision']} ({len(sigs)} signals): {out['reason']}")
    return out


def _decide(conn, run_id, sup, start, end, sigs, ids, stats, trace_id) -> dict:
    def candidate(decision, reason=None, r=None):
        stats["candidates"] += 1
        stats["discarded"] += decision == "discarded"
        return conn.execute(
            """INSERT INTO alert_candidates (run_id, supplier_id, rule_id, method, window_start, window_end,
                 signal_ids, score, decision, discard_reason, llm_confidence, llm_reasoning, latency_ms, trace_id)
               VALUES (%s,%s,'llm_weekly_review','llm',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (run_id, sup["id"], start, end, ids,
             round(10 * r["confidence"] * (SEVERITIES.index(r["severity"]) + 1) / 4, 2) if r else 0,
             decision, reason, r and r["confidence"], r and r["reasoning"], r and r["latency_ms"], trace_id)
        ).fetchone()["id"]

    # An open LLM alert that overlaps or directly precedes this window is the same ongoing situation
    open_alert = conn.execute(
        "SELECT * FROM alerts WHERE method='llm' AND supplier_id=%s AND status <> 'closed' "
        "AND window_end >= %s - interval '1 day' AND window_start <= %s ORDER BY id DESC LIMIT 1",
        (sup["id"], start, end)).fetchone()

    try:
        r = llm.generate_json(SYSTEM, _prompt(sup, sigs, start, end, open_alert), _validate, num_predict=400,
                              span_name="llm.weekly_review")
    except llm.LLMError as e:
        candidate("discarded", f"llm_error: {e}"[:300])
        stats["errors"] += 1
        return {"decision": "error", "reason": str(e)}

    base = {"confidence": r["confidence"], "severity": r["severity"], "latency_ms": r["latency_ms"],
            "reasoning": r["reasoning"], "open_alert_in_context": open_alert["id"] if open_alert else None}
    if not r["raise_alert"]:
        reason = "LLM: no actionable pattern"
    elif r["confidence"] < MIN_ALERT_CONFIDENCE:
        reason = f"LLM confidence {r['confidence']:.2f} below {MIN_ALERT_CONFIDENCE}"
    elif r["severity"] == "low":
        reason = "LLM rated severity low"
    else:
        reason = None
    if reason:
        candidate("discarded", reason, r)
        return {"decision": "discarded", "reason": f"{reason}. {r['reasoning']}", **base}

    title = r["title"] or f"{sup['name']} — LLM weekly review"
    if open_alert:
        cid = candidate("updated", None, r)
        merged = sorted(set(open_alert["signal_ids"]) | set(ids))
        sev = max(open_alert["severity"], r["severity"], key=SEVERITIES.index)
        conn.execute(
            """UPDATE alerts SET signal_ids=%s, window_start=LEAST(window_start,%s), window_end=%s,
                 severity=%s, score=GREATEST(score,%s), title=%s, summary=%s, recommended_action=%s,
                 candidate_id=%s WHERE id=%s""",
            (merged, start, end, sev, r["confidence"] * 10, title, r["summary"],
             r["recommended_action"], cid, open_alert["id"]))
        stats["updated"] += 1
        return {"decision": "updated", "alert_id": open_alert["id"], "title": title, **base, "severity": sev,
                "summary": r["summary"], "recommended_action": r["recommended_action"]}
    cid = candidate("alert", None, r)
    aid = conn.execute(
        """INSERT INTO alerts (candidate_id, supplier_id, rule_id, method, title, severity, score,
             summary, signal_ids, window_start, window_end, recommended_action)
           VALUES (%s,%s,'llm_weekly_review','llm',%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (cid, sup["id"], title, r["severity"], round(r["confidence"] * 10, 2), r["summary"], ids,
         start, end, r["recommended_action"])).fetchone()["id"]
    stats["alerts"] += 1
    return {"decision": "alert", "alert_id": aid, "title": title, **base,
            "summary": r["summary"], "recommended_action": r["recommended_action"]}


def run(as_of: datetime | None = None, days: int = 7, backfill: bool = False,
        suppliers: list[str] | None = None, verbose: bool = True) -> dict:
    init_schema()
    stats = dict(scanned=0, candidates=0, alerts=0, updated=0, discarded=0, skipped=0, errors=0)
    with connect() as conn:
        run_id = conn.execute(
            "INSERT INTO correlation_runs (rules_version, method, model) VALUES (%s,'llm',%s) RETURNING run_id",
            (PROMPT_VERSION, OLLAMA_MODEL)).fetchone()["run_id"]
        sups = conn.execute("SELECT * FROM suppliers ORDER BY id").fetchall()
        if suppliers:
            sups = [s for s in sups if s["id"] in {x.upper() for x in suppliers}]
        end = as_of or datetime.now(timezone.utc)
        if backfill:
            first = conn.execute("SELECT min(published_at) AS t FROM signals").fetchone()["t"] or end
            ends = []
            while end > first:
                ends.append(end)
                end -= timedelta(days=days)
            ends.reverse()  # oldest window first, so later windows can update earlier alerts
        else:
            ends = [end]
        conn.commit()

        for e in ends:
            for sup in sups:
                review_window(conn, run_id, sup, e - timedelta(days=days), e, stats, verbose)
                conn.commit()

        conn.execute(
            """UPDATE correlation_runs SET finished_at=now(), signals_scanned=%s, candidates=%s,
                 alerts_raised=%s, discarded=%s, status=%s WHERE run_id=%s""",
            (stats["scanned"], stats["candidates"], stats["alerts"] + stats["updated"], stats["discarded"],
             "ok" if not stats["errors"] else "partial", run_id))
    if verbose:
        print(f"LLM correlation run {run_id}: {stats}")
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--as-of", help="end of the window, YYYY-MM-DD (default: now)")
    ap.add_argument("--days", type=int, default=7, help="window length in days (default 7)")
    ap.add_argument("--backfill", action="store_true", help="review every week back to the first signal")
    ap.add_argument("--suppliers", nargs="+", help="only these suppliers, e.g. APEX VERDANT")
    a = ap.parse_args()
    as_of = (datetime.fromisoformat(a.as_of).replace(tzinfo=timezone.utc) + timedelta(days=1)
             if a.as_of else None)
    run(as_of, a.days, a.backfill, a.suppliers)
