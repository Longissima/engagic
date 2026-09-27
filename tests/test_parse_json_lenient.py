"""Coverage for the GLM malformed-JSON repairs in analysis.llm.backends.

Z.AI native offers json_object mode only (no json_schema), so the model is
told the shape in prose and is free to approximate it. Every repair here is a
shape observed in production; each test is the fixture that caused it.
"""

import json

import pytest

from analysis.llm.backends import parse_json_lenient

WELL_FORMED = '{"summary_markdown":"The board approved the levy.","topics":["budget"]}'


def test_well_formed_passes_through():
    assert parse_json_lenient(WELL_FORMED)["topics"] == ["budget"]


def test_repair_escaped_topic_delimiters():
    text = '{"summary_markdown":"Levy approved.","topics":[\\"budget\\"]}'
    assert parse_json_lenient(text)["topics"] == ["budget"]


def test_repair_raw_quotes_in_summary():
    text = '{"summary_markdown":"The word "benefitted" appears raw.","topics":["budget"]}'
    assert parse_json_lenient(text)["topics"] == ["budget"]
    assert "benefitted" in parse_json_lenient(text)["summary_markdown"]


def test_repair_stray_backtick_after_object():
    assert parse_json_lenient(WELL_FORMED + "`")["topics"] == ["budget"]


def test_repair_preamble_before_object():
    text = "Here is the JSON you requested:\n" + WELL_FORMED
    assert parse_json_lenient(text)["topics"] == ["budget"]


def test_repair_duplicate_closing_brace():
    """centennialsdMN_5bf0327e_18643446, 2026-09-22: GLM emitted a complete
    object then a lone "}" on the next line. rfind("}") lands on the stray
    brace, so the brace-trimming repair is a no-op and only raw_decode saves
    the summary."""
    text = WELL_FORMED + "\n}"
    result = parse_json_lenient(text)
    assert result["topics"] == ["budget"]
    assert result["summary_markdown"] == "The board approved the levy."


def test_repair_arbitrary_trailing_data():
    assert parse_json_lenient(WELL_FORMED + '\n{"topics":["other"]}')["topics"] == ["budget"]


def test_unrepairable_still_raises():
    with pytest.raises(json.JSONDecodeError):
        parse_json_lenient('{"summary_markdown": "truncated mid')


def test_no_object_at_all_raises():
    with pytest.raises(json.JSONDecodeError):
        parse_json_lenient("I cannot summarize this document.")
