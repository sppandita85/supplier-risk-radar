"""Ingestion job: read files from data/input/<SUPPLIER>/, classify with the LLM, store in Postgres.

Every update is stored, whether it was kept as a signal or discarded as noise.
Processed files move to data/processed/, unreadable ones to data/failed/.
"""
import argparse
import hashlib
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from . import llm, tracing
from .config import FAILED_DIR, INPUT_DIR, MIN_CONFIDENCE, OLLAMA_MODEL, PROCESSED_DIR
from .db import connect, init_schema


def _parse(path: Path, supplier_id: str) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        d = json.loads(text)
        if not d.get("body") and not d.get("title"):
            raise ValueError("update has no title or body")
    else:  # plain text: first line is the title
        lines = text.strip().splitlines()
        if not lines:
            raise ValueError("empty file")
        d = {"title": lines[0], "body": "\n".join(lines[1:]), "source_type": "email",
             "published_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()}
    d["supplier_id"] = d.get("supplier_id") or supplier_id
    d["file_hash"] = hashlib.sha256(
        f"{d['supplier_id']}|{d.get('published_at','')}|{d.get('title','')}|{d.get('body','')}".encode()
    ).hexdigest()
    return d


def _move(path: Path, dest_root: Path) -> None:
    dest = dest_root / path.parent.name
    dest.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), dest / path.name)


def _process_file(path: Path, run_id: int, suppliers: dict, stats: dict, trace_id: str) -> dict:
    """Parse, dedupe, classify and store one file. Returns a summary used for logs and the trace."""
    supplier_id = path.parent.name.upper()
    with connect() as conn:
        try:
            upd = _parse(path, supplier_id)
            if upd["supplier_id"] not in suppliers:
                raise ValueError(f"unknown supplier {upd['supplier_id']}")
        except Exception as e:
            conn.execute("INSERT INTO ingestion_failures (run_id, source_file, supplier_id, stage, error)"
                         " VALUES (%s,%s,%s,'parse',%s)", (run_id, path.name, supplier_id, str(e)))
            stats["failed"] += 1
            _move(path, FAILED_DIR)
            return {"outcome": "failed_parse", "error": str(e)}

        row = conn.execute(
            """INSERT INTO raw_updates (run_id, supplier_id, source_file, source_type, published_at,
                                        title, body, file_hash)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (file_hash) DO NOTHING RETURNING id""",
            (run_id, upd["supplier_id"], path.name, upd.get("source_type"), upd.get("published_at"),
             upd.get("title"), upd.get("body"), upd["file_hash"])).fetchone()
        if row is None:  # same content already ingested
            stats["duplicates"] += 1
            _move(path, PROCESSED_DIR)
            return {"outcome": "duplicate", "title": upd.get("title")}
        update_id = row["id"]

        try:
            c = llm.classify(suppliers[upd["supplier_id"]], upd)
        except llm.LLMError as e:
            conn.execute("INSERT INTO ingestion_failures (run_id, source_file, supplier_id, stage, error)"
                         " VALUES (%s,%s,%s,'llm',%s)", (run_id, path.name, supplier_id, str(e)))
            conn.execute(
                """INSERT INTO classifications (update_id, is_signal, discard_reason, model,
                     prompt_version, error, attempts, trace_id) VALUES (%s,false,'llm_error',%s,%s,%s,2,%s)""",
                (update_id, OLLAMA_MODEL, llm.PROMPT_VERSION, str(e), trace_id))
            stats["failed"] += 1
            conn.commit()
            _move(path, FAILED_DIR)
            return {"outcome": "failed_llm", "error": str(e), "title": upd.get("title")}

        # Post-rules: a "signal" the model isn't sure about is kept as noise
        is_signal, reason = c["is_signal"], None
        if not c["is_signal"]:
            reason = "llm_noise"
        elif c["confidence"] < MIN_CONFIDENCE:
            is_signal, reason = False, "low_confidence"
        conn.execute(
            """INSERT INTO classifications (update_id, is_signal, llm_is_signal, category, severity,
                 sentiment, confidence, reasoning, discard_reason, model, prompt_version, latency_ms,
                 prompt_tokens, output_tokens, attempts, trace_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (update_id, is_signal, c["is_signal"], c["category"], c["severity"], c["sentiment"],
             c["confidence"], c["reasoning"], reason, c["model"], llm.PROMPT_VERSION, c["latency_ms"],
             c["prompt_tokens"], c["output_tokens"], c["attempts"], trace_id))
        stats["ok"] += 1
        stats["signals" if is_signal else "noise"] += 1
    _move(path, PROCESSED_DIR)
    return {"outcome": "signal" if is_signal else "noise", "update_id": update_id, "title": upd.get("title"),
            "category": c["category"], "severity": c["severity"], "confidence": c["confidence"],
            "discard_reason": reason, "reasoning": c["reasoning"], "latency_ms": c["latency_ms"]}


def pending_files(suppliers: list[str] | None = None) -> list[Path]:
    keep = {s.upper() for s in suppliers} if suppliers else None
    return sorted(p for p in INPUT_DIR.glob("*/*") if p.suffix in (".json", ".txt")
                  and (keep is None or p.parent.name.upper() in keep))


def run(limit: int | None = None, verbose: bool = True, suppliers: list[str] | None = None,
        max_seconds: float | None = None) -> dict:
    init_schema()
    deadline = time.monotonic() + max_seconds if max_seconds else None
    files = pending_files(suppliers)
    files = files[:limit] if limit else files
    with connect() as conn:
        suppliers = {r["id"]: r for r in conn.execute("SELECT * FROM suppliers")}
        run_id = conn.execute(
            "INSERT INTO ingestion_runs (files_seen, model) VALUES (%s, %s) RETURNING run_id",
            (len(files), OLLAMA_MODEL)).fetchone()["run_id"]
    stats = dict(ok=0, failed=0, duplicates=0, signals=0, noise=0)

    for i, path in enumerate(files, 1):
        if deadline and time.monotonic() > deadline:
            if verbose:
                print(f"Time limit reached; {len(files) - i + 1} files left in input/")
            break
        sid = path.parent.name.upper()
        with tracing.span("ingest.update", kind="CHAIN", input=path.name, run_id=run_id,
                          supplier=sid, file=path.name) as sp:
            out = _process_file(path, run_id, suppliers, stats, tracing.trace_id(sp))
            tracing.set_output(sp, out, ok=out["outcome"] in ("signal", "noise", "duplicate"))
            for k in ("outcome", "category", "severity", "discard_reason"):
                if out.get(k) is not None:
                    sp.set_attribute(f"radar.{k}", out[k])
        if verbose:
            tag = (f"SIGNAL {out['category']}/sev{out['severity']}" if out["outcome"] == "signal"
                   else f"noise ({out['discard_reason']})" if out["outcome"] == "noise" else out["outcome"].upper())
            print(f"[{i}/{len(files)}] {sid:<8} {tag:<28} {out.get('latency_ms', '')}ms  "
                  f"{out.get('title') or out.get('error', '')}"[:150])

    status = "ok" if stats["failed"] == 0 else ("failed" if stats["ok"] == 0 and files else "partial")
    with connect() as conn:
        conn.execute(
            """UPDATE ingestion_runs SET finished_at=now(), files_ok=%s, files_failed=%s, duplicates=%s,
                 signals=%s, noise=%s, status=%s WHERE run_id=%s""",
            (stats["ok"], stats["failed"], stats["duplicates"], stats["signals"], stats["noise"],
             status, run_id))
    if verbose:
        print(f"Ingestion run {run_id}: {stats}")
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, help="process at most N files")
    ap.add_argument("--suppliers", nargs="+", help="only these supplier folders, e.g. APEX VERDANT")
    ap.add_argument("--max-seconds", type=float, help="stop cleanly after this many seconds")
    a = ap.parse_args()
    run(a.limit, suppliers=a.suppliers, max_seconds=a.max_seconds)
