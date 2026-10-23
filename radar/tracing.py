"""Phoenix (Arize) observability via OpenTelemetry + OpenInference conventions.

Traces go to the Phoenix container over OTLP/HTTP (default http://localhost:6006/v1/traces),
into one project (default "supplier-risk-radar"). Set TRACING=off to disable.
If Phoenix is down, spans are dropped in the background and the radar keeps working.
"""
import json
import os
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from . import config  # noqa: F401  (loads .env before the settings below are read)

PHOENIX_ENDPOINT = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006").rstrip("/")
PHOENIX_PROJECT = os.getenv("PHOENIX_PROJECT", "supplier-risk-radar")
ENABLED = os.getenv("TRACING", "on").lower() not in ("off", "0", "false")

_ready = False


def init() -> None:
    """Register the Phoenix tracer provider once per process (safe to call repeatedly)."""
    global _ready
    if _ready or not ENABLED:
        return
    try:
        from phoenix.otel import register
        register(endpoint=f"{PHOENIX_ENDPOINT}/v1/traces", project_name=PHOENIX_PROJECT,
                 protocol="http/protobuf", batch=True, verbose=False)
    except Exception as e:  # tracing must never break the pipeline
        print(f"[tracing] disabled: {e}")
    _ready = True


def _tracer():
    init()
    return trace.get_tracer("risk-radar")


def _val(v):
    """OTel attributes accept only primitives; everything else becomes JSON."""
    if v is None or isinstance(v, (str, bool, int, float)):
        return v
    return json.dumps(v, default=str)


@contextmanager
def span(name: str, kind: str = "CHAIN", input=None, session_id: str | None = None, **attrs):
    """Start an OpenInference span. kind: CHAIN | LLM | TOOL | AGENT."""
    with _tracer().start_as_current_span(name) as s:
        s.set_attribute("openinference.span.kind", kind)
        if input is not None:
            s.set_attribute("input.value", _val(input))
            s.set_attribute("input.mime_type", "text/plain" if isinstance(input, str) else "application/json")
        if session_id:
            s.set_attribute("session.id", session_id)
        meta = {k: v for k, v in attrs.items() if v is not None}
        if meta:
            s.set_attribute("metadata", _val(meta))
            for k, v in meta.items():
                s.set_attribute(f"radar.{k}", _val(v))
        try:
            yield s
        except Exception as e:
            s.record_exception(e)
            s.set_status(Status(StatusCode.ERROR, str(e)))
            raise


def set_output(s, output, ok: bool = True) -> None:
    s.set_attribute("output.value", _val(output))
    s.set_attribute("output.mime_type", "text/plain" if isinstance(output, str) else "application/json")
    if ok:
        s.set_status(Status(StatusCode.OK))


def _set_messages(s, prefix: str, messages: list[dict]) -> None:
    for i, m in enumerate(messages):
        p = f"{prefix}.{i}.message"
        s.set_attribute(f"{p}.role", m.get("role", ""))
        if m.get("content"):
            s.set_attribute(f"{p}.content", str(m["content"]))
        for j, tc in enumerate(m.get("tool_calls") or []):
            fn = tc.get("function", {})
            s.set_attribute(f"{p}.tool_calls.{j}.tool_call.function.name", fn.get("name", ""))
            s.set_attribute(f"{p}.tool_calls.{j}.tool_call.function.arguments", _val(fn.get("arguments", {})))
        if m.get("role") == "tool" and m.get("tool_name"):
            s.set_attribute(f"{p}.name", m["tool_name"])


def llm_request(s, model: str, messages: list[dict], params: dict, tools: list | None = None) -> None:
    s.set_attribute("llm.model_name", model)
    s.set_attribute("llm.provider", "ollama")
    s.set_attribute("llm.system", "llama")
    s.set_attribute("llm.invocation_parameters", _val(params))
    _set_messages(s, "llm.input_messages", messages)
    for i, t in enumerate(tools or []):
        s.set_attribute(f"llm.tools.{i}.tool.json_schema", _val(t))
    s.set_attribute("input.value", _val({"messages": messages}))
    s.set_attribute("input.mime_type", "application/json")


def llm_response(s, message: dict, prompt_tokens: int | None, output_tokens: int | None) -> None:
    _set_messages(s, "llm.output_messages", [message])
    if prompt_tokens is not None:
        s.set_attribute("llm.token_count.prompt", prompt_tokens)
    if output_tokens is not None:
        s.set_attribute("llm.token_count.completion", output_tokens)
    if prompt_tokens is not None and output_tokens is not None:
        s.set_attribute("llm.token_count.total", prompt_tokens + output_tokens)
    set_output(s, message.get("content") or _val(message.get("tool_calls")) or "")


def trace_id(s) -> str:
    return format(s.get_span_context().trace_id, "032x")


_project_id = None


def phoenix_project_url() -> str:
    """Link to this project in the Phoenix UI (falls back to the Phoenix home page)."""
    global _project_id
    if _project_id is None:
        try:
            import requests
            projects = requests.get(f"{PHOENIX_ENDPOINT}/v1/projects", timeout=2).json()["data"]
            _project_id = next((p["id"] for p in projects if p["name"] == PHOENIX_PROJECT), "")
        except Exception:
            return PHOENIX_ENDPOINT
    return f"{PHOENIX_ENDPOINT}/projects/{_project_id}" if _project_id else PHOENIX_ENDPOINT


def phoenix_trace_url(tid: str) -> str:
    base = phoenix_project_url()
    return f"{base}/traces/{tid}" if "/projects/" in base and tid else base
