"""Unit tests for the cascade router: ladders, hints, audit summaries.

No fixtures required — these always run.
"""

import pytest

from vendors.adapters.parsers import router


@pytest.fixture(autouse=True)
def fresh_hint_registry(monkeypatch):
    monkeypatch.setattr(router, "_city_hints", {})


class TestResolveRungs:
    def test_named_ladder_expands(self):
        assert router.resolve_rungs("agenda") == ["v2:url", "v1:url", "v2:auto"]

    def test_hint_promoted_to_front(self):
        assert router.resolve_rungs("agenda", hint="v1:url") == [
            "v1:url", "v2:url", "v2:auto",
        ]

    def test_hint_not_in_ladder_ignored(self):
        assert router.resolve_rungs("packet", hint="v1:url") == ["v2:toc"]

    def test_hint_already_first_is_noop(self):
        assert router.resolve_rungs("agenda", hint="v2:url") == [
            "v2:url", "v1:url", "v2:auto",
        ]

    def test_explicit_rung_list_passes_through(self):
        assert router.resolve_rungs(["v1:toc", "v2:toc"]) == ["v1:toc", "v2:toc"]

    def test_ladder_not_mutated_by_hint(self):
        before = list(router.LADDERS["agenda"])
        router.resolve_rungs("agenda", hint="v2:auto")
        assert router.LADDERS["agenda"] == before


class TestForceMethodMapping:
    @pytest.mark.parametrize("force,ladder", [
        (None, "auto"),
        ("toc", "packet"),
        ("url", "url_legacy"),
        ("v2_url", "v2_url_only"),
    ])
    def test_legacy_mappings(self, force, ladder):
        assert router.ladder_for_force_method(force) == ladder

    def test_unknown_force_method_raises(self):
        with pytest.raises(ValueError):
            router.ladder_for_force_method("bogus")


class TestHintRegistry:
    def test_set_get_roundtrip(self):
        router.set_city_hint("granicus", "oakley", "agenda", "v1:url")
        assert router.get_city_hint("granicus", "oakley", "agenda") == "v1:url"

    def test_keyed_per_ladder(self):
        router.set_city_hint("granicus", "oakley", "agenda", "v1:url")
        assert router.get_city_hint("granicus", "oakley", "packet") is None

    def test_empty_values_ignored(self):
        router.set_city_hint("granicus", "oakley", "agenda", "")
        assert router.get_city_hint("granicus", "oakley", "agenda") is None

    def test_seed_bulk(self):
        n = router.seed_city_hints([
            {"vendor": "granicus", "slug": "a", "ladder": "agenda", "rung": "v2:url"},
            {"vendor": "legistar", "slug": "b", "ladder": "packet", "rung": "v2:toc"},
        ])
        assert n == 2
        assert router.get_city_hint("legistar", "b", "packet") == "v2:toc"


class TestSummarizeRuns:
    def test_single_winning_run(self):
        runs = [{"ladder": "packet", "winning_rung": "v2:toc",
                 "parse_method": "v2_toc", "item_count": 9,
                 "failure_reason": None, "attempts": []}]
        s = router.summarize_runs(runs)
        assert s["winning_rung"] == "v2:toc"
        assert s["winning_ladder"] == "packet"
        assert s["item_count"] == 9
        assert s["failure_reason"] is None

    def test_last_winner_wins_across_runs(self):
        runs = [
            {"ladder": "agenda", "winning_rung": "v2:url",
             "parse_method": "v2_url", "item_count": 2, "attempts": []},
            {"ladder": "packet", "winning_rung": "v2:toc",
             "parse_method": "v2_toc", "item_count": 14, "attempts": []},
        ]
        s = router.summarize_runs(runs)
        assert s["winning_rung"] == "v2:toc"
        assert s["winning_ladder"] == "packet"

    def test_no_winner_carries_last_failure(self):
        runs = [
            {"ladder": "agenda", "winning_rung": None,
             "failure_reason": "no_items", "attempts": []},
            {"ladder": "packet", "winning_rung": None,
             "failure_reason": "no_text_layer", "attempts": []},
        ]
        s = router.summarize_runs(runs)
        assert s["winning_rung"] is None
        assert s["failure_reason"] == "no_text_layer"
        assert len(s["runs"]) == 2


class TestChunkPdfEdges:
    def test_unopenable_path_classified(self, tmp_path):
        bogus = tmp_path / "not_a_pdf.pdf"
        bogus.write_bytes(b"definitely not a pdf" * 100)
        result = router.chunk_pdf(str(bogus), "packet")
        assert result.failure_reason == router.OPEN_FAILED
        assert result.winning_rung is None

    def test_audit_includes_ladder(self, tmp_path):
        bogus = tmp_path / "x.pdf"
        bogus.write_bytes(b"nope" * 200)
        audit = router.chunk_pdf(str(bogus), "packet").audit()
        assert audit["ladder"] == "packet"
        assert audit["failure_reason"] == router.OPEN_FAILED
