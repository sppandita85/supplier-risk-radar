"""Signal-vs-noise classification with a local Ollama model."""
import json
import time

import requests

from . import tracing
from .config import LLM_TIMEOUT, OLLAMA_MODEL, OLLAMA_URL

PROMPT_VERSION = "v1"

CATEGORIES = ["financial", "operational", "geopolitical", "compliance", "esg",
              "cyber", "logistics", "quality", "none"]

SYSTEM = f"""You are a supply-chain risk analyst. You read one update about a supplier and decide
whether it is a RISK SIGNAL (could disrupt supply, cost, quality, compliance or reputation for the
buyer) or NOISE (marketing, routine news, awards, hiring posts, unrelated mentions, positive or neutral
business-as-usual information).

Respond ONLY with JSON of this exact shape:
{{"is_signal": true|false,
  "category": one of {CATEGORIES},
  "severity": integer 1-5 (1 = minor, 5 = supply stopped / existential; use 1 for noise),
  "sentiment": number from -1 (very negative) to 1 (very positive),
  "confidence": number 0-1 for how sure you are of is_signal,
  "reasoning": one short sentence}}

Examples:
- "Supplier wins regional sustainability award" -> noise, category none, severity 1.
- "Supplier launches new website and LinkedIn campaign" -> noise.
- "Fire at main plant halts production for two weeks" -> signal, operational, severity 5.
- "Rating agency places supplier on negative credit watch" -> signal, financial, severity 4.
- "Customs authority opens investigation into export documentation" -> signal, compliance, severity 3.
- "Shipment PO-1182 delayed 9 days, root cause under review" -> signal, logistics, severity 3.
- "Supplier announces 4% price increase next quarter" -> signal, financial, severity 2.
- "PO-2210 delayed 14 days due to production stoppage" -> signal, logistics, severity 3.
"""


class LLMError(Exception):
    pass


def _validate(d: dict) -> dict:
    if not isinstance(d.get("is_signal"), bool):
        if str(d.get("is_signal")).lower() in ("true", "false"):
            d["is_signal"] = str(d["is_signal"]).lower() == "true"
        else:
            raise LLMError(f"bad is_signal: {d.get('is_signal')!r}")
    cat = str(d.get("category", "none")).lower().strip()
    d["category"] = cat if cat in CATEGORIES else "none"
    d["severity"] = max(1, min(5, int(round(float(d.get("severity", 1))))))
    d["sentiment"] = max(-1.0, min(1.0, float(d.get("sentiment", 0))))
    d["confidence"] = max(0.0, min(1.0, float(d.get("confidence", 0.5))))
    d["reasoning"] = str(d.get("reasoning", ""))[:500]
    return d


def generate_json(system: str, prompt: str, validate, retries: int = 1, num_predict: int = 200,
                  span_name: str = "ollama.generate") -> dict:
    """Call Ollama in JSON mode, validate the reply, retry once on bad output.
    Each attempt is traced to Phoenix as an LLM span."""
    last_err = None
    t0 = time.perf_counter()
    options = {"temperature": 0, "num_predict": num_predict, "format": "json"}
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    for attempt in range(1, retries + 2):
        with tracing.span(span_name, kind="LLM", attempt=attempt) as s:
            tracing.llm_request(s, OLLAMA_MODEL, messages, options)
            try:
                r = requests.post(
                    f"{OLLAMA_URL}/api/generate",
                    json={"model": OLLAMA_MODEL, "system": system, "prompt": prompt, "format": "json",
                          "stream": False, "options": {"temperature": 0, "num_predict": num_predict}},
                    timeout=LLM_TIMEOUT,
                )
                r.raise_for_status()
                body = r.json()
                tracing.llm_response(s, {"role": "assistant", "content": body.get("response", "")},
                                     body.get("prompt_eval_count"), body.get("eval_count"))
                result = validate(json.loads(body["response"]))
                result.update(latency_ms=int((time.perf_counter() - t0) * 1000),
                              prompt_tokens=body.get("prompt_eval_count"),
                              output_tokens=body.get("eval_count"), attempts=attempt, model=OLLAMA_MODEL)
                return result
            except (requests.RequestException, ValueError, KeyError, TypeError, LLMError) as e:
                last_err = e
                s.record_exception(e)
                s.set_status(tracing.Status(tracing.StatusCode.ERROR, str(e)))
    raise LLMError(f"{type(last_err).__name__}: {last_err}")


def classify(supplier: dict, update: dict, retries: int = 1) -> dict:
    """Return validated classification dict plus telemetry (latency_ms, tokens, attempts, model)."""
    prompt = (
        f"Supplier: {supplier['name']} ({supplier['category']}, {supplier['country']}, "
        f"criticality {supplier['criticality']}/5)\n"
        f"Source: {update.get('source_type', 'unknown')}\n"
        f"Date: {update.get('published_at', '')}\n"
        f"Title: {update.get('title', '')}\n"
        f"Body: {update.get('body', '')}\n"
    )
    return generate_json(SYSTEM, prompt, _validate, retries, span_name="llm.classify_update")
