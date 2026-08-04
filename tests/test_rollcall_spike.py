"""Regression tests for the deterministic roll-call feasibility spike."""

import importlib.util
import sys
from pathlib import Path


ROLLCALL_DIR = Path(__file__).parents[1] / "scripts" / "spikes" / "rollcall"


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROLLCALL_DIR / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rollcall_parse = _load_module("rollcall_parse_for_test", "parse.py")
rollcall_fetch = _load_module("rollcall_fetch_for_test", "fetch.py")


def _passage(sections):
    return rollcall_parse.Passage(
        matter_file="26-0001",
        motion_text="synthetic vote",
        action="ADOPTED",
        outcome="PASS",
        sections=sections,
    )


def test_publish_gate_accepts_one_category_per_member():
    passage = _passage([
        ("AYE", ["Alice Smith"], 1),
        ("NO", ["Bob Jones"], 1),
    ])

    result = passage.evaluate_publish_gate(
        rollcall_parse.Gazetteer(["Alice Smith", "Bob Jones"])
    )

    assert result.publishable
    assert result.duplicate_members == []


def test_publish_gate_rejects_member_in_conflicting_categories():
    passage = _passage([
        ("AYE", ["Alice Smith"], 1),
        ("NO", ["Alice Smith"], 1),
    ])

    result = passage.evaluate_publish_gate(
        rollcall_parse.Gazetteer(["Alice Smith"])
    )

    assert not result.publishable
    assert result.duplicate_members == [("Alice Smith", ["AYE", "NO"])]
    assert "members appear more than once" in result.reasons[-1]


def test_publish_gate_rejects_duplicate_within_one_category():
    passage = _passage([("AYE", ["Alice Smith", "Alice Smith"], 2)])

    result = passage.evaluate_publish_gate(
        rollcall_parse.Gazetteer(["Alice Smith"])
    )

    assert not result.publishable
    assert result.duplicate_members == [("Alice Smith", ["AYE", "AYE"])]


def test_prepare_output_dirs_supports_clean_checkout(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    pdfs = tmp_path / "pdfs"
    out = tmp_path / "out"
    monkeypatch.setattr(rollcall_fetch, "CACHE", cache)
    monkeypatch.setattr(rollcall_fetch, "PDFS", pdfs)
    monkeypatch.setattr(rollcall_fetch, "OUT", out)

    rollcall_fetch.prepare_output_dirs()

    assert cache.is_dir()
    assert pdfs.is_dir()
    assert out.is_dir()
