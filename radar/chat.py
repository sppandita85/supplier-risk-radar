"""Chat agent for business users: question -> local LLM -> read-only tools over Postgres -> answer.

The LLM never writes SQL. It can only call the fixed, read-only tools below, and every
conversation turn is logged to chat_log for the Operations tab.

CLI:  python -m radar.chat "Which suppliers have critical alerts?"
"""
import json
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from . import tracing
from .config import LLM_TIMEOUT, OLLAMA_MODEL, OLLAMA_URL
from .db import connect
from .llm import CATEGORIES

MAX_STEPS = 4      # tool-call rounds before we force an answer
MAX_ROWS = 25      # rows returned to the LLM per tool call

SYSTEM = f"""You are the Supplier Risk Radar assistant for business users (procurement and supply-chain managers).
Today is {datetime.now(timezone.utc):%Y-%m-%d}. The radar monitors 5 suppliers: Apex Semiconductors (APEX),
Nordic Steelworks (NORDIC), Meridian Logistics (MERIDIAN), Verdant Chemicals (VERDANT), Sunrise Textiles (SUNRISE).

Rules:
- ALWAYS use the tools to get facts. Never invent alerts, numbers, dates or suppliers.
- Only pass tool filters the user explicitly asked for. Do NOT filter by method unless the user
  asks specifically about rule-based or LLM alerts. "Right now" / "currently" means status 'open';
  do not use the days filter for that.
- Alerts come from two methods: 'rules' (rule engine) and 'llm' (weekly LLM review). If both flag the same
  incident, say so instead of counting it twice.
- Never mention tools, functions or JSON in the answer; just state the facts.
- Answer in plain business language: short, direct, with alert ids (#N), dates and severities.
  Use a short bullet list when listing several items. End with a recommended next step when relevant.
- If the tools return nothing, say there is no data for that, and mention which suppliers do have data.
"""

# ---- tools ---------------------------------------------------------------------------------------


def _supplier_id(conn, supplier: str | None) -> str | None:
    if not supplier:
        return None
    row = conn.execute("SELECT id FROM suppliers WHERE id ILIKE %s OR name ILIKE %s ORDER BY id LIMIT 1",
                       (supplier.strip(), f"%{supplier.strip()}%")).fetchone()
    if not row:
        raise ValueError(f"unknown supplier '{supplier}'. Known: APEX, NORDIC, MERIDIAN, VERDANT, SUNRISE")
    return row["id"]


def list_alerts(supplier: str = None, severity: str = None, status: str = None, method: str = None,
                days: int = None) -> list[dict]:
    with connect() as conn:
        sql = ["SELECT a.id, s.name AS supplier, a.method, a.severity, a.status, a.title, a.window_start::date AS "
               "from_date, a.window_end::date AS to_date, cardinality(a.signal_ids) AS signals "
               "FROM alerts a JOIN suppliers s ON s.id = a.supplier_id WHERE true"]
        args = []
        if sid := _supplier_id(conn, supplier):
            sql.append("AND a.supplier_id = %s"); args.append(sid)
        if severity:
            sql.append("AND a.severity = %s"); args.append(severity.lower())
        if status:
            sql.append("AND a.status = %s"); args.append(status.lower())
        if method:
            sql.append("AND a.method = %s"); args.append(method.lower())
        if days and int(days) > 0:
            sql.append("AND a.window_end >= %s"); args.append(datetime.now(timezone.utc) - timedelta(days=int(days)))
        sql.append("ORDER BY a.window_end DESC LIMIT %s"); args.append(MAX_ROWS)
        return conn.execute(" ".join(sql), args).fetchall()


def get_alert_details(alert_id: int) -> dict:
    with connect() as conn:
        a = conn.execute("SELECT a.*, s.name AS supplier FROM alerts a JOIN suppliers s ON s.id=a.supplier_id "
                         "WHERE a.id = %s", (int(alert_id),)).fetchone()
        if not a:
            raise ValueError(f"alert #{alert_id} not found")
        sigs = conn.execute(
            "SELECT update_id AS signal_id, published_at::date AS date, source_type, category, severity, "
            "title, reasoning FROM signals WHERE update_id = ANY(%s) ORDER BY published_at",
            (a["signal_ids"],)).fetchall()
        keep = ["id", "supplier", "method", "rule_id", "severity", "status", "score", "title", "summary",
                "recommended_action", "window_start", "window_end", "created_at"]
        return {**{k: a[k] for k in keep}, "contributing_signals": sigs}


def get_supplier_signals(supplier: str, days: int = 30, category: str = None) -> dict:
    with connect() as conn:
        sid = _supplier_id(conn, supplier)
        since = datetime.now(timezone.utc) - timedelta(days=int(days or 30))
        sql = ("SELECT update_id AS signal_id, published_at::date AS date, source_type, category, severity, "
               "round(confidence::numeric, 2) AS confidence, title FROM signals "
               "WHERE supplier_id = %s AND published_at >= %s")
        args = [sid, since]
        if category and category.lower() in CATEGORIES:  # ignore 'all', 'any', etc.
            sql += " AND category = %s"; args.append(category.lower())
        rows = conn.execute(sql + " ORDER BY published_at DESC LIMIT %s", args + [MAX_ROWS]).fetchall()
        noise = conn.execute(
            "SELECT count(*) AS n FROM classifications c JOIN raw_updates u ON u.id=c.update_id "
            "WHERE u.supplier_id=%s AND u.published_at >= %s AND NOT c.is_signal", (sid, since)).fetchone()["n"]
        return {"supplier": sid, "window_days": days, "signals": rows, "noise_updates_discarded": noise}


def get_risk_overview() -> list[dict]:
    with connect() as conn:
        return conn.execute("""
            SELECT s.id, s.name, s.criticality,
                   (SELECT count(*) FROM raw_updates u WHERE u.supplier_id = s.id) AS updates_ingested,
                   (SELECT count(*) FROM signals g WHERE g.supplier_id = s.id) AS signals,
                   (SELECT round(sum(g.severity * g.confidence)::numeric, 1) FROM signals g
                     WHERE g.supplier_id = s.id) AS risk_points,
                   (SELECT count(*) FROM alerts a WHERE a.supplier_id = s.id AND a.status <> 'closed') AS open_alerts,
                   (SELECT max(a.severity) FILTER (WHERE a.severity = 'critical') FROM alerts a
                     WHERE a.supplier_id = s.id) AS has_critical,
                   (SELECT max(g.published_at)::date FROM signals g WHERE g.supplier_id = s.id) AS last_signal
            FROM suppliers s ORDER BY risk_points DESC NULLS LAST""").fetchall()


def get_discarded_decisions(supplier: str = None, limit: int = 15) -> list[dict]:
    with connect() as conn:
        sql = ("SELECT c.id, s.name AS supplier, c.method, c.rule_id, c.window_end::date AS date, "
               "c.discard_reason, c.llm_reasoning FROM alert_candidates c JOIN suppliers s ON s.id=c.supplier_id "
               "WHERE c.decision = 'discarded'")
        args = []
        if sid := _supplier_id(conn, supplier):
            sql += " AND c.supplier_id = %s"; args.append(sid)
        return conn.execute(sql + " ORDER BY c.id DESC LIMIT %s", args + [min(int(limit or 15), MAX_ROWS)]).fetchall()


TOOLS = {
    "list_alerts": (list_alerts, "List risk alerts, newest first. Use for 'which alerts', 'how many alerts', "
                    "'critical alerts', 'alerts for supplier X'.", {
                        "supplier": {"type": "string", "description": "supplier name or id, e.g. 'Apex' or 'APEX'"},
                        "severity": {"type": "string", "enum": ["medium", "high", "critical"]},
                        "status": {"type": "string", "enum": ["open", "acknowledged", "closed"]},
                        "method": {"type": "string", "enum": ["rules", "llm"],
                                   "description": "ONLY if the user explicitly asks for rule-based or LLM alerts"},
                        "days": {"type": "integer", "description": "only alerts active in the last N days"}}, []),
    "get_alert_details": (get_alert_details, "Full details of one alert: summary, recommended action and "
                          "the signals that caused it. Use for 'why', 'what happened', 'tell me about alert N'.", {
                              "alert_id": {"type": "integer", "description": "alert id, e.g. 9"}}, ["alert_id"]),
    "get_supplier_signals": (get_supplier_signals, "Recent risk signals for one supplier, plus how many noise "
                             "updates were discarded.", {
                                 "supplier": {"type": "string", "description": "supplier name or id"},
                                 "days": {"type": "integer", "description": "look-back window, default 30"},
                                 "category": {"type": "string", "description": "financial, operational, logistics, "
                                              "compliance, esg, cyber, quality or geopolitical"}}, ["supplier"]),
    "get_risk_overview": (get_risk_overview, "Risk overview of all 5 suppliers: signals, risk points, open alerts. "
                          "Use for 'which supplier is riskiest', 'overall status'.", {}, []),
    "get_discarded_decisions": (get_discarded_decisions, "Patterns the radar evaluated but did NOT raise as alerts, "
                                "with the reason. Use for 'why was X not flagged'.", {
                                    "supplier": {"type": "string"},
                                    "limit": {"type": "integer"}}, []),
}

TOOL_SPECS = [{"type": "function", "function": {
    "name": name, "description": desc,
    "parameters": {"type": "object", "properties": props, "required": req}}}
    for name, (_, desc, props, req) in TOOLS.items()]


def run_tool(name: str, args: dict) -> str:
    with tracing.span(f"tool.{name}", kind="TOOL", input=args) as sp:
        sp.set_attribute("tool.name", name)
        sp.set_attribute("tool.parameters", json.dumps(args or {}, default=str))
        if name not in TOOLS:
            out, ok = json.dumps({"error": f"unknown tool {name}"}), False
        else:
            fn, desc, props, _ = TOOLS[name]
            sp.set_attribute("tool.description", desc)
            clean = {k: v for k, v in (args or {}).items() if k in props and v not in (None, "", "null")}
            try:
                out, ok = json.dumps(fn(**clean), default=str)[:6000], True
            except Exception as e:  # tool errors go back to the LLM so it can recover
                out, ok = json.dumps({"error": str(e)}), False
                sp.record_exception(e)
                sp.set_status(tracing.Status(tracing.StatusCode.ERROR, str(e)))
        tracing.set_output(sp, out, ok=ok)
        return out


# ---- agent loop ----------------------------------------------------------------------------------


def _chat(messages: list[dict], tools: bool = True) -> dict:
    params = {"temperature": 0}
    with tracing.span("llm.chat", kind="LLM") as sp:
        tracing.llm_request(sp, OLLAMA_MODEL, messages, params, TOOL_SPECS if tools else None)
        r = requests.post(f"{OLLAMA_URL}/api/chat", timeout=LLM_TIMEOUT, json={
            "model": OLLAMA_MODEL, "messages": messages, "stream": False,
            "options": params, **({"tools": TOOL_SPECS} if tools else {})})
        r.raise_for_status()
        body = r.json()
        tracing.llm_response(sp, body["message"], body.get("prompt_eval_count"), body.get("eval_count"))
        return body["message"]


def ask(question: str, history: list[dict] | None = None, session_id: str = "cli") -> dict:
    """Answer a question. Returns {answer, tool_calls: [{name, args, result}], latency_ms, trace_id}."""
    with tracing.span("chat.ask", kind="AGENT", input=question, session_id=session_id) as sp:
        out = _ask(question, history, session_id, tracing.trace_id(sp))
        tracing.set_output(sp, out["answer"], ok=out["error"] is None)
        sp.set_attribute("radar.tools_used", json.dumps([t["name"] for t in out["tool_calls"]]))
        if out["error"]:
            sp.set_status(tracing.Status(tracing.StatusCode.ERROR, out["error"]))
    return out


def _ask(question: str, history: list[dict] | None, session_id: str, trace_id: str) -> dict:
    t0 = time.perf_counter()
    messages = [{"role": "system", "content": SYSTEM}, *(history or [])[-6:],
                {"role": "user", "content": question}]
    trace, answer, error = [], "", None
    try:
        for step in range(MAX_STEPS + 1):
            msg = _chat(messages, tools=step < MAX_STEPS)
            calls = msg.get("tool_calls") or []
            if not calls:
                answer = msg.get("content", "").strip()
                break
            messages.append({"role": "assistant", "content": msg.get("content", ""), "tool_calls": calls})
            for c in calls:
                name, args = c["function"]["name"], c["function"].get("arguments") or {}
                if isinstance(args, str):
                    args = json.loads(args or "{}")
                result = run_tool(name, args)
                trace.append({"name": name, "args": args, "result": result})
                messages.append({"role": "tool", "tool_name": name, "content": result})
        if not answer:
            answer = "Sorry, I couldn't produce an answer. Try rephrasing the question."
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        answer = "The assistant is unavailable right now (is Ollama running?)."
    latency = int((time.perf_counter() - t0) * 1000)
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO chat_log (session_id, question, answer, tools_used, tool_calls, latency_ms, model,"
                " error, trace_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (session_id, question, answer, [t["name"] for t in trace],
                 json.dumps([{k: t[k] for k in ("name", "args")} for t in trace]), latency, OLLAMA_MODEL, error,
                 trace_id))
    except Exception:
        pass  # logging must never break the chat
    return {"answer": answer, "tool_calls": trace, "latency_ms": latency, "error": error, "trace_id": trace_id}


if __name__ == "__main__":
    out = ask(" ".join(sys.argv[1:]) or "Which suppliers have open critical alerts?")
    for t in out["tool_calls"]:
        print(f"🔧 {t['name']}({t['args']})")
    print(f"\n{out['answer']}\n\n({out['latency_ms']} ms)")
