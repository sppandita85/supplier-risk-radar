"""Supplier Risk Radar dashboard.   streamlit run dashboard/app.py"""
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import psycopg
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from radar import chat, tracing  # noqa: E402
from radar.config import INPUT_DIR, PG_DSN  # noqa: E402

st.set_page_config(page_title="Supplier Risk Radar", page_icon="📡", layout="wide")

# ---- palette (fixed order; color follows the entity) -------------------------
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
MUTED, GRID, INK2 = "#898781", "#e1e0d9", "#52514e"
SEQ = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
STATUS = {"medium": "#fab219", "high": "#ec835a", "critical": "#d03b3b"}
STATUS_ICON = {"medium": "🟡", "high": "🟠", "critical": "🔴"}
SIGNAL_NOISE = {"Signal": SERIES[0], "Noise": "#c3c2b7"}


def style(fig, height=320):
    fig.update_layout(height=height, margin=dict(l=8, r=8, t=36, b=8), plot_bgcolor="#fcfcfb",
                      paper_bgcolor="#fcfcfb", font=dict(color="#0b0b0b", size=12),
                      legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0, title=None),
                      hoverlabel=dict(bgcolor="white"), bargap=0.25)
    fig.update_xaxes(gridcolor=GRID, linecolor="#c3c2b7", title=None)
    fig.update_yaxes(gridcolor=GRID, linecolor="#c3c2b7", zeroline=False)
    return fig


@st.cache_data(ttl=30)
def q(sql: str, params: tuple = ()) -> pd.DataFrame:
    with psycopg.connect(PG_DSN) as conn:
        cur = conn.execute(sql, params)
        cols = [d.name for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


# ---- data ---------------------------------------------------------------------
suppliers = q("SELECT * FROM suppliers ORDER BY id")
if suppliers.empty:
    st.error("No data yet. Run `python -m radar.db`, `python -m radar.synth`, `python -m radar.ingest`.")
    st.stop()
SUP_COLOR = {n: SERIES[i] for i, n in enumerate(suppliers["name"])}

updates = q("""SELECT u.id, u.supplier_id, s.name AS supplier, u.source_type, u.published_at, u.received_at,
                      u.title, u.body, u.source_file, c.is_signal, c.llm_is_signal, c.category, c.severity,
                      c.sentiment, c.confidence, c.reasoning, c.discard_reason, c.latency_ms,
                      c.prompt_tokens, c.output_tokens, c.attempts, c.error, c.model, c.classified_at
               FROM raw_updates u JOIN suppliers s ON s.id = u.supplier_id
               LEFT JOIN classifications c ON c.update_id = u.id""")
alerts = q("""SELECT a.*, s.name AS supplier FROM alerts a JOIN suppliers s ON s.id = a.supplier_id
              ORDER BY a.created_at DESC""")
cands = q("""SELECT c.*, s.name AS supplier FROM alert_candidates c JOIN suppliers s ON s.id = c.supplier_id
             WHERE c.rule_id <> 'episode'""")
ing_runs = q("SELECT * FROM ingestion_runs ORDER BY run_id")
cor_runs = q("SELECT * FROM correlation_runs ORDER BY run_id")
failures = q("SELECT * FROM ingestion_failures ORDER BY failed_at DESC")
chats = q("SELECT * FROM chat_log ORDER BY asked_at DESC")

for df, col in [(updates, "published_at"), (updates, "classified_at"), (alerts, "window_end")]:
    if not df.empty:
        df[col] = pd.to_datetime(df[col], utc=True)
updates["kind"] = updates["is_signal"].map({True: "Signal", False: "Noise"})

# ---- sidebar filters -------------------------------------------------------------
with st.sidebar:
    st.title("📡 Risk Radar")
    sel = st.multiselect("Suppliers", list(suppliers["name"]), default=list(suppliers["name"]))
    pmin = updates["published_at"].min().date() if not updates.empty else date.today() - timedelta(days=60)
    rng = st.date_input("Published between", (pmin, date.today()))
    if st.button("↻ Refresh data"):
        st.cache_data.clear()
        st.rerun()
    st.link_button("🔭 Open Phoenix traces", tracing.phoenix_project_url(), width="stretch")
    st.caption(f"Model: {updates['model'].dropna().iloc[-1] if updates['model'].notna().any() else '—'}")

d0, d1 = (rng if isinstance(rng, tuple) and len(rng) == 2 else (pmin, date.today()))
in_range = updates["published_at"].dt.date.between(d0, d1) & updates["supplier"].isin(sel)
U = updates[in_range]
S = U[U["is_signal"] == True]  # noqa: E712
A = alerts[alerts["supplier"].isin(sel)]

st.title("Supplier Risk Radar")
tabs = st.tabs(["⚙️ Operations", "🧭 Overview", "🔎 Supplier deep dive", "🗂️ Risk categories", "🚨 Alerts",
                "💬 Ask the radar"])

# =============================================================================
# 1. OPERATIONS — how the radar itself is performing
# =============================================================================
with tabs[0]:
    st.subheader("Pipeline health")
    backlog = len(list(INPUT_DIR.glob("*/*")))
    lat = updates["latency_ms"].dropna()
    c = st.columns(6)
    c[0].metric("Updates ingested", len(updates))
    c[1].metric("Kept as signal", int(updates["is_signal"].fillna(False).sum()),
                f"{updates['is_signal'].mean():.0%} of total" if len(updates) else None, delta_color="off")
    c[2].metric("Discarded as noise", int((updates["is_signal"] == False).sum()))  # noqa: E712
    c[3].metric("Failed files", len(failures))
    c[4].metric("LLM latency p50 / p95", f"{lat.quantile(.5)/1000:.1f}s / {lat.quantile(.95)/1000:.1f}s"
                if len(lat) else "—")
    c[5].metric("Backlog in input/", backlog)

    l, r = st.columns(2)
    with l:
        by_day = (updates.assign(day=updates["published_at"].dt.date)
                  .groupby(["day", "kind"]).size().reset_index(name="updates"))
        fig = px.bar(by_day, x="day", y="updates", color="kind", color_discrete_map=SIGNAL_NOISE,
                     title="Signal vs noise per day (by publish date)")
        st.plotly_chart(style(fig), width="stretch")
    with r:
        reasons = (updates.assign(outcome=updates["discard_reason"].fillna("kept as signal"))
                   .groupby("outcome").size().reset_index(name="n").sort_values("n"))
        fig = px.bar(reasons, x="n", y="outcome", orientation="h", title="Classification outcomes",
                     color_discrete_sequence=[SERIES[0]], text="n")
        st.plotly_chart(style(fig), width="stretch")

    l, r = st.columns(2)
    with l:
        fig = px.histogram(updates.dropna(subset=["latency_ms"]), x="latency_ms", nbins=40,
                           title="LLM latency distribution (ms)", color_discrete_sequence=[SERIES[0]])
        if len(lat):
            fig.add_vline(x=lat.quantile(.95), line_dash="dot", line_color=INK2,
                          annotation_text="p95", annotation_position="top")
        st.plotly_chart(style(fig), width="stretch")
    with r:
        conf = updates.dropna(subset=["confidence"])
        fig = px.histogram(conf, x="confidence", color="kind", nbins=20, barmode="stack",
                           color_discrete_map=SIGNAL_NOISE, title="LLM confidence distribution")
        fig.add_vline(x=0.5, line_dash="dot", line_color=INK2, annotation_text="keep threshold")
        st.plotly_chart(style(fig), width="stretch")

    l, r = st.columns(2)
    with l:
        src = (updates.groupby(["source_type", "kind"]).size().reset_index(name="n"))
        fig = px.bar(src, x="source_type", y="n", color="kind", color_discrete_map=SIGNAL_NOISE,
                     title="Signal yield by source type")
        st.plotly_chart(style(fig), width="stretch")
    with r:
        if not cands.empty:
            cm = (cands.assign(outcome=cands["decision"].replace({"alert": "raised alert",
                                                                   "updated": "updated alert"}),
                               method=cands["method"].map({"rules": "Rules", "llm": "LLM weekly review"}))
                  .groupby(["method", "outcome"]).size().reset_index(name="n"))
            fig = px.bar(cm, x="n", y="method", color="outcome", orientation="h", text="n",
                         color_discrete_map={"raised alert": SERIES[0], "updated alert": SERIES[6],
                                             "discarded": "#c3c2b7"},
                         title="Correlation decisions: rules vs LLM")
            st.plotly_chart(style(fig), width="stretch")

    llm_c = cands[cands["method"] == "llm"] if not cands.empty else cands
    if not llm_c.empty:
        st.subheader("LLM correlation (weekly supplier review)")
        c = st.columns(4)
        c[0].metric("Supplier-weeks reviewed", len(llm_c))
        c[1].metric("Alert rate", f"{llm_c['decision'].isin(['alert', 'updated']).mean():.0%}")
        c[2].metric("Avg review latency", f"{llm_c['latency_ms'].mean() / 1000:.1f}s")
        c[3].metric("Errors", int(llm_c["discard_reason"].fillna("").str.startswith("llm_error").sum()))
        st.dataframe(llm_c.sort_values(["window_end", "supplier"], ascending=[False, True])[
            ["supplier", "window_start", "window_end", "signal_ids", "decision", "llm_confidence",
             "llm_reasoning", "discard_reason", "latency_ms", "trace_id"]], width="stretch", hide_index=True,
            column_config={"window_start": st.column_config.DatetimeColumn(format="YYYY-MM-DD"),
                           "window_end": st.column_config.DatetimeColumn(format="YYYY-MM-DD")})

    if not cands.empty:
        dr = (cands[cands["decision"] == "discarded"]
              .assign(reason=lambda d: d["discard_reason"].str.replace(r"\d+(\.\d+)?", "N", regex=True)
                      .str.replace(r"#N", "#", regex=False))
              .groupby(["method", "rule_id", "reason"]).size().reset_index(name="n").sort_values("n", ascending=False))
        st.markdown("**Why correlation candidates were discarded**")
        st.dataframe(dr, width="stretch", hide_index=True)

    st.markdown("**Ingestion runs**")
    st.dataframe(ing_runs, width="stretch", hide_index=True)
    st.markdown("**Correlation runs**")
    st.dataframe(cor_runs, width="stretch", hide_index=True)
    if not failures.empty:
        st.markdown("**Failed files**")
        st.dataframe(failures, width="stretch", hide_index=True)

    if not chats.empty:
        st.subheader("Business-user chat")
        c = st.columns(4)
        c[0].metric("Questions asked", len(chats))
        c[1].metric("Avg response time", f"{chats['latency_ms'].mean() / 1000:.1f}s")
        c[2].metric("Answered without tools", int((chats["tools_used"].str.len() == 0).sum()),
                    help="Answers given without fetching data: check these for made-up facts")
        c[3].metric("Errors", int(chats["error"].notna().sum()))
        l, r = st.columns(2)
        with l:
            tu = chats.explode("tools_used").dropna(subset=["tools_used"]) \
                      .groupby("tools_used").size().reset_index(name="calls").sort_values("calls")
            if not tu.empty:
                fig = px.bar(tu, x="calls", y="tools_used", orientation="h", text="calls",
                             title="Tools called by the chat assistant", color_discrete_sequence=[SERIES[0]])
                st.plotly_chart(style(fig, 260), width="stretch")
        with r:
            st.markdown("**Recent questions**")
            st.dataframe(chats[["asked_at", "question", "tools_used", "latency_ms", "error", "trace_id"]].head(15),
                         width="stretch", hide_index=True, height=260)

# =============================================================================
# 2. OVERVIEW
# =============================================================================
with tabs[1]:
    open_a = A[A["status"] != "closed"]
    c = st.columns(4)
    c[0].metric("Open alerts", len(open_a))
    c[1].metric("Critical alerts", int((open_a["severity"] == "critical").sum()))
    c[2].metric("Risk signals (filtered)", len(S))
    top = (S.groupby("supplier").apply(lambda d: (d["severity"] * d["confidence"]).sum(),
                                        include_groups=False).sort_values(ascending=False))
    c[3].metric("Highest-risk supplier", top.index[0] if len(top) else "—")

    st.subheader("Supplier risk scorecard")
    sc = suppliers.rename(columns={"name": "supplier"})[["supplier", "category", "country", "tier", "criticality"]]
    agg = S.groupby("supplier").agg(signals=("id", "count"), avg_severity=("severity", "mean"),
                                    max_severity=("severity", "max"), last_signal=("published_at", "max"))
    risk = S.assign(pts=S["severity"] * S["confidence"]).groupby("supplier")["pts"].sum().rename("risk_points")
    oa = open_a.groupby("supplier").size().rename("open_alerts")
    sc = sc.set_index("supplier").join([agg, risk, oa]).fillna({"signals": 0, "risk_points": 0, "open_alerts": 0})
    sc["risk_index"] = (sc["risk_points"] * (0.8 + 0.1 * sc["criticality"])).round(1)
    sc = sc.sort_values("risk_index", ascending=False).reset_index()
    st.dataframe(sc, width="stretch", hide_index=True,
                 column_config={"risk_index": st.column_config.ProgressColumn(
                     "Risk index", min_value=0, max_value=float(max(sc["risk_index"].max(), 1)), format="%.1f"),
                     "avg_severity": st.column_config.NumberColumn(format="%.1f"),
                     "last_signal": st.column_config.DatetimeColumn(format="YYYY-MM-DD")})

    l, r = st.columns(2)
    with l:
        wk = (S.assign(week=S["published_at"].dt.tz_localize(None).dt.to_period("W").dt.start_time)
              .groupby(["week", "supplier"]).size().reset_index(name="signals"))
        fig = px.line(wk, x="week", y="signals", color="supplier", markers=True,
                      color_discrete_map=SUP_COLOR, title="Risk signals per week")
        fig.update_traces(line_width=2, marker_size=8)
        st.plotly_chart(style(fig), width="stretch")
    with r:
        fig = px.bar(sc.sort_values("risk_index"), x="risk_index", y="supplier", orientation="h",
                     text="risk_index", title="Risk index (severity × confidence × criticality)",
                     color_discrete_sequence=[SERIES[0]])
        st.plotly_chart(style(fig), width="stretch")

# =============================================================================
# 3. SUPPLIER DEEP DIVE
# =============================================================================
with tabs[2]:
    who = st.selectbox("Supplier", sel or list(suppliers["name"]))
    info = suppliers[suppliers["name"] == who].iloc[0]
    st.caption(f"{info['category']} · {info['country']} · tier {info['tier']} · criticality {info['criticality']}/5")
    su, ss = U[U["supplier"] == who], S[S["supplier"] == who]
    c = st.columns(4)
    c[0].metric("Updates", len(su)); c[1].metric("Signals", len(ss))
    c[2].metric("Noise discarded", int((su["is_signal"] == False).sum()))  # noqa: E712
    c[3].metric("Alerts", int((A["supplier"] == who).sum()))

    fig = go.Figure()
    noise = su[su["is_signal"] == False]  # noqa: E712
    fig.add_trace(go.Scatter(x=noise["published_at"], y=["noise"] * len(noise), mode="markers", name="Noise",
                             marker=dict(size=8, color="#c3c2b7"), text=noise["title"],
                             hovertemplate="%{x|%b %d}<br>%{text}<extra>noise</extra>"))
    fig.add_trace(go.Scatter(
        x=ss["published_at"], y=ss["category"], mode="markers", name="Signal (size & shade = severity)",
        marker=dict(size=ss["severity"] * 5 + 4, color=ss["severity"], colorscale=SEQ[2:], cmin=1, cmax=5,
                    line=dict(width=2, color="#fcfcfb"), showscale=True, colorbar=dict(title="sev", len=0.6)),
        text=ss["title"], customdata=ss[["severity", "confidence"]],
        hovertemplate="%{x|%b %d}<br><b>%{text}</b><br>sev %{customdata[0]} · conf %{customdata[1]:.2f}<extra></extra>"))
    for _, a in A[A["supplier"] == who].iterrows():
        fig.add_vrect(x0=a["window_start"], x1=a["window_end"] + pd.Timedelta(hours=12),
                      fillcolor=STATUS[a["severity"]], opacity=0.12, line_width=0,
                      annotation_text=f"Alert #{a['id']} ({a['severity']})", annotation_position="top left")
    fig.update_layout(title="Timeline: signals by category, with alert windows")
    st.plotly_chart(style(fig, 380), width="stretch")

    st.markdown("**All updates with LLM reasoning**")
    st.dataframe(su.sort_values("published_at", ascending=False)[
        ["published_at", "kind", "source_type", "title", "category", "severity", "confidence",
         "reasoning", "discard_reason"]], width="stretch", hide_index=True)

# =============================================================================
# 4. RISK CATEGORIES
# =============================================================================
with tabs[3]:
    hm = S.pivot_table(index="supplier", columns="category", values="id", aggfunc="count", fill_value=0)
    if not hm.empty:
        fig = px.imshow(hm, text_auto=True, color_continuous_scale=SEQ, aspect="auto",
                        title="Signals by supplier × risk category")
        fig.update_traces(xgap=2, ygap=2)
        st.plotly_chart(style(fig, 340), width="stretch")

        l, r = st.columns(2)
        with l:
            sev = S.pivot_table(index="supplier", columns="category", values="severity", aggfunc="mean")
            fig = px.imshow(sev.round(1), text_auto=True, color_continuous_scale=SEQ, zmin=1, zmax=5,
                            aspect="auto", title="Average severity by supplier × category")
            fig.update_traces(xgap=2, ygap=2)
            st.plotly_chart(style(fig), width="stretch")
        with r:
            tot = S.groupby("category").agg(signals=("id", "count"), avg_sev=("severity", "mean")) \
                   .reset_index().sort_values("signals")
            fig = px.bar(tot, x="signals", y="category", orientation="h", text="signals",
                         title="Signals per category", color_discrete_sequence=[SERIES[0]],
                         hover_data={"avg_sev": ":.1f"})
            st.plotly_chart(style(fig), width="stretch")

        cats = S["category"].value_counts().index[:6].tolist()
        tr = (S.assign(week=S["published_at"].dt.tz_localize(None).dt.to_period("W").dt.start_time,
                       category=S["category"].where(S["category"].isin(cats), "other"))
              .groupby(["week", "category"]).size().reset_index(name="signals"))
        fig = px.bar(tr, x="week", y="signals", facet_col="category", facet_col_wrap=4,
                     color_discrete_sequence=[SERIES[0]], title="Weekly trend per category")
        fig.for_each_annotation(lambda a: a.update(text=a.text.split("=")[-1]))
        st.plotly_chart(style(fig, 420), width="stretch")
    else:
        st.info("No signals in the current filter.")

# =============================================================================
# 5. ALERTS
# =============================================================================
with tabs[4]:
    c = st.columns(4)
    for i, sv in enumerate(["critical", "high", "medium"]):
        c[i].metric(f"{STATUS_ICON[sv]} {sv.title()}", int((A["severity"] == sv).sum()))
    c[3].metric("Candidates discarded", int(((cands["decision"] == "discarded") &
                                             cands["supplier"].isin(sel)).sum()) if not cands.empty else 0)

    l, r = st.columns(2)
    with l:
        if not A.empty:
            fig = px.bar(A.groupby(["supplier", "severity"]).size().reset_index(name="alerts"),
                         x="supplier", y="alerts", color="severity", color_discrete_map=STATUS,
                         title="Alerts by supplier and severity")
            st.plotly_chart(style(fig), width="stretch")
    with r:
        if not cands.empty:
            cs = cands[cands["supplier"].isin(sel)]
            dec = cs.assign(outcome=cs["decision"].replace({"updated": "alert"})) \
                    .groupby(["rule_id", "outcome"]).size().reset_index(name="n")
            fig = px.bar(dec, x="n", y="rule_id", color="outcome", orientation="h",
                         color_discrete_map={"alert": SERIES[0], "discarded": "#c3c2b7"},
                         title="Rule evaluations: promoted vs discarded")
            st.plotly_chart(style(fig), width="stretch")

    st.subheader("Alert register")
    meth = st.radio("Raised by", ["All", "Rules", "LLM"], horizontal=True)
    AR = A if meth == "All" else A[A["method"] == meth.lower()]
    for _, a in AR.iterrows():
        badge = "🤖 LLM" if a["method"] == "llm" else "📏 Rules"
        with st.expander(f"{STATUS_ICON.get(a['severity'], '⚪')} #{a['id']} · {badge} · {a['title']} · "
                         f"{a['severity']} · {a['status']}"):
            st.write(a["summary"])
            if a.get("recommended_action"):
                st.markdown(f"**Recommended action:** {a['recommended_action']}")
            st.caption(f"Window {pd.to_datetime(a['window_start']):%Y-%m-%d} → "
                       f"{pd.to_datetime(a['window_end']):%Y-%m-%d} · {a['rule_id']} · score {a['score']:.1f}")
            contrib = updates[updates["id"].isin(a["signal_ids"])].sort_values("published_at")
            st.dataframe(contrib[["published_at", "source_type", "title", "category", "severity", "confidence",
                                  "reasoning"]], width="stretch", hide_index=True)
    if AR.empty:
        st.info("No alerts for the selected suppliers.")

    with st.expander("All correlation candidates (incl. discarded)"):
        st.dataframe(cands[cands["supplier"].isin(sel)].sort_values("id", ascending=False),
                     width="stretch", hide_index=True)

# =============================================================================
# 6. ASK THE RADAR — chat: question -> local LLM -> read-only tools -> answer
# =============================================================================
with tabs[5]:
    import uuid

    st.caption("Ask about alerts, suppliers and signals in plain English. The local LLM fetches the facts "
               "with read-only tools before answering. Click 🔧 under an answer to see what it looked up.")
    ss = st.session_state
    ss.setdefault("chat_session", uuid.uuid4().hex[:8])
    ss.setdefault("chat_msgs", [])

    examples = ["Which suppliers have critical alerts right now?", "Tell me about alert 9 and what I should do",
                "Which supplier is the riskiest overall?", "Why was Verdant not flagged?"]
    ex_cols = st.columns(len(examples))
    clicked = next((e for i, e in enumerate(examples) if ex_cols[i].button(e, key=f"ex{i}", width="stretch")), None)

    box = st.container(height=520)
    with box:
        for m in ss.chat_msgs:
            with st.chat_message(m["role"]):
                st.markdown(m["content"])
                if m.get("trace_id"):
                    st.markdown(f"[🔭 View trace in Phoenix]({tracing.phoenix_trace_url(m['trace_id'])})")
                if m.get("tools"):
                    with st.expander(f"🔧 {len(m['tools'])} tool call(s) · {m['latency_ms'] / 1000:.1f}s"):
                        for t in m["tools"]:
                            st.code(f"{t['name']}({t['args']})", language="python")
                            st.json(t["result"][:3000], expanded=False)

    typed = st.chat_input("Ask about alerts, suppliers or signals…")
    question = typed or clicked
    if question:
        history = [{"role": m["role"], "content": m["content"]} for m in ss.chat_msgs]
        ss.chat_msgs.append({"role": "user", "content": question})
        with box:
            with st.chat_message("user"):
                st.markdown(question)
            with st.chat_message("assistant"):
                with st.spinner("Looking it up…"):
                    out = chat.ask(question, history, session_id=ss.chat_session)
        ss.chat_msgs.append({"role": "assistant", "content": out["answer"], "tools": out["tool_calls"],
                             "latency_ms": out["latency_ms"], "trace_id": out.get("trace_id")})
        st.rerun()

    if ss.chat_msgs and st.button("Clear conversation"):
        ss.chat_msgs = []
        st.rerun()
