"""Unit tests for reading update files."""
import json

import pytest

from radar import ingest


def _write(tmp_path, name, content):
    d = tmp_path / "APEX"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(content)
    return p


def test_parse_json(tmp_path):
    p = _write(tmp_path, "a.json", json.dumps({"title": "Fire", "body": "Plant halted",
                                               "published_at": "2026-10-01T00:00:00+00:00"}))
    d = ingest._parse(p, "APEX")
    assert d["supplier_id"] == "APEX" and d["title"] == "Fire" and len(d["file_hash"]) == 64


def test_parse_text_uses_first_line_as_title(tmp_path):
    d = ingest._parse(_write(tmp_path, "b.txt", "FW: Delay\nPO late by 3 days\n"), "APEX")
    assert d["title"] == "FW: Delay" and d["body"] == "PO late by 3 days" and d["source_type"] == "email"


@pytest.mark.parametrize("content", ['{"title": "Trunc', "{}", ""])
def test_parse_rejects_bad_files(tmp_path, content):
    suffix = ".txt" if content == "" else ".json"
    with pytest.raises(ValueError):
        ingest._parse(_write(tmp_path, f"bad{suffix}", content), "APEX")


def test_same_text_on_different_dates_is_not_a_duplicate(tmp_path):
    a = ingest._parse(_write(tmp_path, "a.json", json.dumps({"title": "Hiring", "published_at": "2026-10-01"})), "APEX")
    b = ingest._parse(_write(tmp_path, "b.json", json.dumps({"title": "Hiring", "published_at": "2026-10-02"})), "APEX")
    c = ingest._parse(_write(tmp_path, "c.json", json.dumps({"title": "Hiring", "published_at": "2026-10-01"})), "APEX")
    assert a["file_hash"] != b["file_hash"]
    assert a["file_hash"] == c["file_hash"]
