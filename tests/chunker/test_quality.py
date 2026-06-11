"""Quality layer: garbage-title lint, repair strategies, segmentation smell."""

import pytest

import corpus_lib
from vendors.adapters.parsers import quality as q


class TestClassifyTitle:
    @pytest.mark.parametrize("title,label", [
        ("Staff Report 2026-0615.pdf", "filename"),
        ("- Cover Page", "cover_page"),
        ("Attachment", "generic_label"),
        ("900-6833", "numeric_or_date"),
        ("", "empty_or_tiny"),
        ("Approval of Minutes from March 11", None),
        ("PUBLIC HEARINGS", None),
    ])
    def test_patterns(self, title, label):
        assert q.classify_title(title) == label


class TestFilenameRepair:
    @pytest.mark.parametrize("filename,expected", [
        ("2026-412 Agenda Item - Water Shortage Update 2026-0615.pdf",
         "Water Shortage Update"),
        ("BPAC Agenda Packet 20260610.pdf", "BPAC Agenda Packet"),
        ("San Rafael CBPP Public Draft (1)-compressed.pdf",
         "San Rafael CBPP Public Draft"),
        ("Budget_Amendment_FY27.docx", "Budget Amendment FY27"),
    ])
    def test_extracts_contained_title(self, filename, expected):
        assert q._title_from_filename(filename) == expected

    def test_pure_date_filename_unrepairable(self):
        # nothing left after stripping -> no repair, stays flagged
        assert q._title_from_filename("03252026.pdf") is None


class TestSubjectHarvest:
    def test_same_line_subject(self):
        text = "TOWN HALL\nSUBJECT: Adoption of the FY27 Budget\nbody"
        assert q._title_from_subject_line(text) == "Adoption of the FY27 Budget"

    def test_label_on_own_line(self):
        # memo headers split label/value across text lines
        text = "STAFF REPORT\nTO:\nMayor and Council\nRE:\nHold interviews and appoint one resident\nbody"
        assert q._title_from_subject_line(text) == "Hold interviews and appoint one resident"

    def test_letterhead_page_yields_nothing(self):
        # no generic first-line fallback — letterhead must not become a title
        text = "City of Menlo Park  701 Laurel St.\nComplete Streets Commission\nREGULAR MEETING AGENDA"
        assert q._title_from_subject_line(text) is None


class TestSegmentationSmell:
    @pytest.mark.parametrize("lines,items,smell", [
        (32, 1, "under_split"),    # monteserenoCA shape
        (18, 4, "under_split"),
        (3, 66, "over_split"),
        (9, 9, None),
        (0, 13, None),             # TOC items with no textual numbering
        (4, 1, None),              # below signal floor
    ])
    def test_smell(self, lines, items, smell):
        assert q.segmentation_smell(lines, items) == smell


# --- corpus -------------------------------------------------------------------

@pytest.mark.skipif(not corpus_lib.GOLDENED, reason="no goldens")
@pytest.mark.parametrize(
    "entry",
    [e for e in corpus_lib.GOLDENED
     if (corpus_lib.load_golden(e) or {}).get("winning_rung")],
    ids=lambda e: e["meeting_id"],
)
def test_quality_matches_golden(entry):
    result = corpus_lib.run_cached(entry)
    golden = corpus_lib.load_golden(entry)
    assert result.quality == golden["quality"], (
        f"quality drift: {result.quality} vs golden {golden['quality']}"
    )


@pytest.mark.skipif(not corpus_lib.FETCHED, reason="no fixtures")
def test_text_extractor_titles_stay_clean():
    """The text engine controls its own titles — zero garbage, always."""
    for entry in corpus_lib.FETCHED:
        result = corpus_lib.run_cached(entry)
        if result.parse_method == "text_items":
            assert result.quality["garbage_titles"] == 0, entry["meeting_id"]
