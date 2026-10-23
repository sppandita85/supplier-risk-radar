"""Unit tests for validating and repairing LLM JSON output, and the Ollama call wrapper."""
import pytest

from radar import correlate_llm, llm


def test_classify_validation_clamps_and_coerces():
    out = llm._validate({"is_signal": "true", "category": "Weird", "severity": 9,
                         "sentiment": -4, "confidence": 1.7, "reasoning": "x" * 900})
    assert out["is_signal"] is True
    assert out["category"] == "none"
    assert out["severity"] == 5 and out["sentiment"] == -1.0 and out["confidence"] == 1.0
    assert len(out["reasoning"]) == 500


def test_classify_validation_rejects_missing_verdict():
    with pytest.raises(llm.LLMError):
        llm._validate({"category": "financial"})


def test_review_validation_cleans_ids_and_severity():
    out = correlate_llm._validate({"raise_alert": "false", "severity": "extreme", "confidence": 0.7,
                                   "key_signal_ids": [3, "#4", "abc"], "risk_themes": "not-a-list"})
    assert out["raise_alert"] is False
    assert out["severity"] == "medium"
    assert out["key_signal_ids"] == [3, 4]
    assert out["risk_themes"] == []


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_generate_json_retries_once_on_bad_json(monkeypatch):
    replies = iter([{"response": "not json"},
                    {"response": '{"is_signal": false}', "prompt_eval_count": 10, "eval_count": 3}])
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: _Resp(next(replies)))
    out = llm.generate_json("sys", "prompt", llm._validate)
    assert out["attempts"] == 2 and out["prompt_tokens"] == 10


def test_generate_json_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: _Resp({"response": "{}"}))
    with pytest.raises(llm.LLMError):
        llm.generate_json("sys", "prompt", llm._validate)
