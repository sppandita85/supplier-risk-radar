"""Correlation job: turn a supplier's series of signals into alerts, or discard them.

1. Per supplier, chain signals into "episodes" (signals no more than EPISODE_GAP_DAYS apart).
2. Each rule in config/rules.yaml evaluates the episode and returns a score.
3. The best rule that matched with score >= alert_threshold raises the alert;
   every other evaluation is stored as a discarded candidate with a reason.
Everything evaluated is written to alert_candidates, so discards can be analysed too.
Re-running is safe: episodes already evaluated are skipped, and episodes that grew
update their existing open alert instead of creating a new one.
"""
from datetime import timedelta

from . import tracing
from .config import load_rules
from .db import connect, init_schema

EPISODE_GAP_DAYS = 10


def _weight(criticality: int) -> float:
    return 0.8 + 0.1 * (criticality or 3)


def _pts(sigs) -> float:
    return sum(s["severity"] * s["confidence"] for s in sigs)


def _windows(sigs, days):
    """Yield every window of signals ending at each signal, spanning `days`."""
    for i, end in enumerate(sigs):
        start_t = end["published_at"] - timedelta(days=days)
        yield [s for s in sigs[: i + 1] if s["published_at"] >= start_t]


# Each rule returns (matched, raw_score, signals_used, reason_if_not_matched)
def rule_burst(sigs, cfg):
    best = max(_windows(sigs, cfg["window_days"]), key=len)
    if len(best) >= cfg["min_signals"]:
        return True, _pts(best), best, None
    return False, _pts(best), best, f"only {len(best)} signals in {cfg['window_days']}d (need {cfg['min_signals']})"


def rule_severity_accumulation(sigs, cfg):
    best = max(_windows(sigs, cfg["window_days"]), key=_pts)
    if len(best) >= cfg["min_signals"]:
        return True, _pts(best), best, None
    return False, _pts(best), best, f"needs {cfg['min_signals']}+ signals in {cfg['window_days']}d"


def rule_multi_category(sigs, cfg):
    best = max(_windows(sigs, cfg["window_days"]), key=lambda w: (len({s["category"] for s in w}), _pts(w)))
    ncat = len({s["category"] for s in best})
    if ncat >= cfg["min_categories"]:
        return True, _pts(best) + 2 * (ncat - 1), best, None
    return False, _pts(best), best, f"only {ncat} risk category in {cfg['window_days']}d"


def rule_escalation(sigs, cfg):
    """Longest run of consecutive signals whose severity never drops and rises overall."""
    best, run = [], [sigs[0]]
    for s in sigs[1:]:
        in_window = s["published_at"] - run[0]["published_at"] <= timedelta(days=cfg["window_days"])
        if s["severity"] >= run[-1]["severity"] and in_window:
            run.append(s)
        else:
            run = [s]
        if len(run) > len(best) and run[-1]["severity"] > run[0]["severity"]:
            best = list(run)
    if len(best) >= cfg["min_steps"]:
        return True, _pts(best) + 3, best, None
    return False, _pts(best or sigs[:1]), best or sigs[:1], "no rising severity trend"


def rule_critical_event(sigs, cfg):
    crit = [s for s in sigs if s["severity"] >= cfg["min_severity"] and s["confidence"] >= cfg["min_confidence"]]
    if crit:
        return True, None, crit, None   # score set to at least the threshold by caller
    return False, 0, sigs[:1], f"no severity-{cfg['min_severity']} event"


RULES = {
    "burst": rule_burst,
    "severity_accumulation": rule_severity_accumulation,
    "multi_category": rule_multi_category,
    "escalation": rule_escalation,
    "critical_event": rule_critical_event,
}


def _episodes(sigs):
    eps, cur = [], []
    for s in sigs:
        if cur and s["published_at"] - cur[-1]["published_at"] > timedelta(days=EPISODE_GAP_DAYS):
            eps.append(cur)
            cur = []
        cur.append(s)
    if cur:
        eps.append(cur)
    return eps


def _alert_severity(score, rule_id):
    if rule_id == "critical_event" or score >= 20:
        return "critical"
    return "high" if score >= 14 else "medium"


def _summary(supplier, sigs, rule_id):
    cats = sorted({s["category"] for s in sigs})
    titles = "; ".join(s["title"] for s in sigs[:4])
    return (f"{supplier['name']}: {len(sigs)} correlated signals ({', '.join(cats)}) "
            f"via rule '{rule_id}'. {titles}")


def run(verbose: bool = True) -> dict:
    with tracing.span("correlate_rules.run", kind="CHAIN", input="rule-based correlation over all signals",
                      rules_version=load_rules()["version"]) as sp:
        stats = _run(verbose)
        tracing.set_output(sp, stats)
    return stats


def _run(verbose: bool = True) -> dict:
    init_schema()
    cfg = load_rules()
    threshold, min_conf = cfg["alert_threshold"], cfg.get("min_confidence", 0.5)
    stats = dict(scanned=0, candidates=0, alerts=0, updated=0, discarded=0, skipped_episodes=0)

    with connect() as conn:
        run_id = conn.execute("INSERT INTO correlation_runs (rules_version) VALUES (%s) RETURNING run_id",
                              (cfg["version"],)).fetchone()["run_id"]
        suppliers = {r["id"]: r for r in conn.execute("SELECT * FROM suppliers")}
        seen = {(r["supplier_id"], tuple(sorted(r["signal_ids"])))
                for r in conn.execute("SELECT DISTINCT supplier_id, signal_ids FROM alert_candidates "
                                      "WHERE rule_id = 'episode'")}

        def add_candidate(sid, rule_id, sigs, score, decision, reason=None):
            stats["candidates"] += 1
            if decision == "discarded":
                stats["discarded"] += 1
            return conn.execute(
                """INSERT INTO alert_candidates (run_id, supplier_id, rule_id, window_start, window_end,
                     signal_ids, score, decision, discard_reason) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING id""",
                (run_id, sid, rule_id, sigs[0]["published_at"], sigs[-1]["published_at"],
                 [s["update_id"] for s in sigs], round(score or 0, 2), decision, reason)).fetchone()["id"]

        for sid, sup in suppliers.items():
            sigs = conn.execute(
                "SELECT * FROM signals WHERE supplier_id=%s AND confidence >= %s AND published_at IS NOT NULL "
                "ORDER BY published_at", (sid, min_conf)).fetchall()
            stats["scanned"] += len(sigs)
            w = _weight(sup["criticality"])

            for ep in _episodes(sigs):
                ids = tuple(sorted(s["update_id"] for s in ep))
                if (sid, ids) in seen:
                    stats["skipped_episodes"] += 1
                    continue
                # Marker row: records that this exact episode was evaluated (for idempotency)
                add_candidate(sid, "episode", ep, _pts(ep) * w, "evaluated")
                stats["candidates"] -= 1

                existing = conn.execute(
                    "SELECT * FROM alerts WHERE method='rules' AND supplier_id=%s AND status <> 'closed' "
                    "AND signal_ids && %s::int[] "
                    "ORDER BY id DESC LIMIT 1", (sid, list(ids))).fetchone()

                if len(ep) == 1 and not rule_critical_event(ep, cfg["rules"]["critical_event"])[0]:
                    add_candidate(sid, "isolated", ep, _pts(ep) * w, "discarded",
                                  "insufficient corroboration (single signal)")
                    continue

                contenders = []
                for rule_id, fn in RULES.items():
                    rcfg = cfg["rules"].get(rule_id, {})
                    if not rcfg.get("enabled", True):
                        continue
                    matched, raw, used, why = fn(ep, rcfg)
                    if rule_id == "critical_event" and matched:
                        score = max(threshold, _pts(ep) * w)
                        used = ep
                    else:
                        score = (raw or 0) * w
                    if not matched:
                        add_candidate(sid, rule_id, used, score, "discarded", why)
                    elif score < threshold:
                        add_candidate(sid, rule_id, used, score, "discarded",
                                      f"score {score:.1f} below threshold {threshold}")
                    else:
                        contenders.append((score, rule_id, used))

                if not contenders:
                    continue
                contenders.sort(key=lambda c: c[0], reverse=True)
                score, rule_id, _ = contenders[0]
                sig_ids = [s["update_id"] for s in ep]

                if existing:
                    cand = add_candidate(sid, rule_id, ep, score, "updated", None)
                    conn.execute(
                        """UPDATE alerts SET signal_ids=%s, window_start=%s, window_end=%s,
                             score=GREATEST(score,%s), severity=%s, summary=%s, candidate_id=%s WHERE id=%s""",
                        (sig_ids, ep[0]["published_at"], ep[-1]["published_at"], round(score, 2),
                         _alert_severity(max(score, existing["score"]), rule_id),
                         _summary(sup, ep, rule_id), cand, existing["id"]))
                    alert_id = existing["id"]
                    stats["updated"] += 1
                else:
                    cand = add_candidate(sid, rule_id, ep, score, "alert")
                    alert_id = conn.execute(
                        """INSERT INTO alerts (candidate_id, supplier_id, rule_id, title, severity, score,
                             summary, signal_ids, window_start, window_end)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (cand, sid, rule_id, f"{sup['name']} — {rule_id.replace('_', ' ')} risk pattern",
                         _alert_severity(score, rule_id), round(score, 2), _summary(sup, ep, rule_id),
                         sig_ids, ep[0]["published_at"], ep[-1]["published_at"])).fetchone()["id"]
                    stats["alerts"] += 1
                for s2, r2, u2 in contenders[1:]:
                    add_candidate(sid, r2, u2, s2, "discarded", f"merged into alert #{alert_id}")
                if verbose:
                    print(f"{sid:<9} ALERT #{alert_id} via {rule_id} score={score:.1f} signals={len(ep)}")

        conn.execute(
            """UPDATE correlation_runs SET finished_at=now(), signals_scanned=%s, candidates=%s,
                 alerts_raised=%s, discarded=%s, status='ok' WHERE run_id=%s""",
            (stats["scanned"], stats["candidates"], stats["alerts"] + stats["updated"],
             stats["discarded"], run_id))
    if verbose:
        print(f"Correlation run {run_id}: {stats}")
    return stats


if __name__ == "__main__":
    run()
