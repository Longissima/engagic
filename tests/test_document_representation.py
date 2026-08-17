import time

from analysis.llm.document_representation import (
    SourceDocument,
    build_compact_representation,
    needs_proactive_representation,
)
from parsing.text_quality import is_garbled_text_layer
from parsing.pdf import PdfExtractor


def _tagged_table(rows: int = 600) -> str:
    values = []
    for index in range(rows):
        values.extend(
            [
                f"[ADDED: Candidate {index}]",
                f"[ADDED: {index}]",
                "[ADDED: 0]",
                "[ADDED: 0]",
            ]
        )
    return "\n\n".join(values)


def test_tagged_table_adapter_preserves_cell_values_and_order():
    source = _tagged_table()
    represented = build_compact_representation(
        [
            SourceDocument(
                "Election canvass",
                source,
                content_sha256="a" * 64,
                source_url="https://example.gov/canvass.pdf",
                page_count=20,
            )
        ]
    )

    assert represented.adapters == ("tagged-table-v1",)
    assert represented.compacted
    assert represented.ratio < 0.55
    assert "@cells" in represented.text
    assert "\n0\n0\n" in represented.text
    assert represented.text.index("Candidate 5") < represented.text.index("Candidate 6")
    assert "sha256=" + "a" * 64 in represented.text
    assert "source=https://example.gov/canvass.pdf" in represented.text


def test_fixed_width_adapter_makes_columns_explicit_and_rle_encodes_cells():
    rows = ["PART          DESCRIPTION          PRICE"]
    rows.extend(f"{index:<14} Widget {index:<12} {index}.00" for index in range(600))
    rows.append("Span " * 100)
    source = "\n".join(rows)

    represented = build_compact_representation([SourceDocument("Schedule", source)])

    assert represented.adapters == ("fixed-width-report-v1",)
    assert "PART\tDESCRIPTION\tPRICE" in represented.text
    assert "Span*100" in represented.text
    assert represented.represented_chars < represented.source_chars


def test_fixed_width_adapter_compacts_repeated_numeric_notation_without_values_lost():
    row = (
        "1          480435.9          3746848.36          "
        "0.03658 480435.9, 3746848.36          1.2E-06"
    )
    source = "\n".join([row] * 600)

    represented = build_compact_representation([SourceDocument("Risk grid", source)])

    assert represented.adapters == ("fixed-width-report-v1",)
    assert "1\t480435.9\t3746848.36\t0.03658 @xy\t1.2e6" in represented.text
    assert "@xy within a row repeats that row's second and third fields" in represented.text
    assert "1.2e6 = 1.2E-06" in represented.text
    assert "VALUE*N" in represented.text


def test_unsupported_prose_remains_raw():
    prose = "The council considered a short agreement. " * 20
    represented = build_compact_representation([SourceDocument("Memo", prose)])

    assert represented.adapters == ("raw",)
    assert not represented.compacted
    assert prose in represented.text


def test_garbled_text_is_not_misclassified_as_a_table():
    garbled = "\x9c:w3¬:9 Ö9R\x9c:8¬wÔÖ\x8e\x9cÖR\x8b9S " * 80

    assert is_garbled_text_layer(garbled)
    represented = build_compact_representation([SourceDocument("Broken PDF", garbled)])
    assert represented.adapters == ("raw",)


def test_normal_prose_and_numeric_tables_are_not_garbled():
    assert not is_garbled_text_layer(
        "This is ordinary public-record prose with names, dates, and decisions. " * 10
    )
    assert not is_garbled_text_layer("100 200 300 400 500\n" * 50)


def test_non_latin_public_record_prose_is_not_garbled():
    samples = [
        "市议会审议了公共交通预算和社区服务合同。" * 30,
        "ناقش مجلس المدينة ميزانية النقل العام وعقد خدمات المجتمع. " * 20,
        "Городской совет рассмотрел бюджет транспорта и договор услуг. " * 20,
    ]

    assert all(not is_garbled_text_layer(sample) for sample in samples)


def test_shorter_readable_ocr_replaces_a_long_garbled_text_layer():
    garbled = "\x9c:w3¬:9 Ö9R\x9c:8¬wÔÖ\x8e\x9cÖR\x8b9S " * 80
    ocr = (
        "The State is awarding this equipment rental contract to the supplier. "
        * 8
    )

    assert len(ocr) < len(garbled)
    assert PdfExtractor(max_ocr_workers=1)._is_ocr_better(garbled, ocr, 1)


def test_numeric_ocr_replaces_garbled_text_without_a_prose_letter_ratio():
    garbled = "\x9c:w3¬:9 Ö9R\x9c:8¬wÔÖ\x8e\x9cÖR\x8b9S " * 80
    numeric_report = "2026 00421 19.50 0.00 33109 775.25\n" * 20

    assert not is_garbled_text_layer(numeric_report)
    assert PdfExtractor(max_ocr_workers=1)._is_ocr_better(
        garbled, numeric_report, 1
    )


def test_suspiciously_large_page_text_is_never_sliced_before_ocr():
    source = "complete embedded source layer " * 8_000 + "END-OF-SOURCE"

    class Page:
        def get_text(self, *, sort=True):
            return source

    class Document:
        def __len__(self):
            return 1

        def __getitem__(self, page_num):
            assert page_num == 0
            return Page()

    extractor = PdfExtractor(
        detect_legislative_formatting=False,
        max_ocr_workers=1,
        ocr_enabled=False,
    )
    result = extractor._extract_from_document(Document(), False, 0.0)

    assert source in result["text"]
    assert result["text"].endswith("END-OF-SOURCE")
    assert result["ocr_pending"] == 1
    assert result["ocr_pending_pages"] == [1]
    assert result["method"] == "pymupdf-partial"


def test_failed_ocr_candidate_is_marked_partial():
    source = "complete embedded source layer " * 8_000

    class Page:
        number = 0

        def get_text(self, *, sort=True):
            return source

    class Document:
        def __len__(self):
            return 1

        def __getitem__(self, page_num):
            assert page_num == 0
            return Page()

    class FailedOcrExtractor(PdfExtractor):
        def _render_page_for_ocr(self, page):
            return b"png", 1, 1

        def _ocr_pages_parallel(self, tasks):
            return (
                {page_num: original for page_num, _, original, _ in tasks},
                {page_num for page_num, _, _, _ in tasks},
            )

    extractor = FailedOcrExtractor(
        detect_legislative_formatting=False,
        max_ocr_workers=1,
    )
    result = extractor._extract_from_document(Document(), False, time.time())

    assert result["ocr_pending"] == 1
    assert result["ocr_pending_pages"] == [1]
    assert result["method"] == "pymupdf-partial"


def test_contract_catalog_receipt_keeps_terms_and_audits_sku_rows():
    header = (
        "    MFGPART           MFGNAME               PRODNAME           "
        "ISSCODE     SIN    PPOINT   GSAPRICE"
    )
    rows = []
    for index in range(1_100):
        if index % 44 == 0:
            rows.append(header)
        rows.append(
            f"PART-{index:<12} MAKER                 PRODUCT {index:<10} "
            f"EA      336320     US      {index + 1}.00"
        )
    source = (
        "--- PAGE 1 ---\nGENERAL SERVICES ADMINISTRATION\n"
        "Contract number: GS-TEST\nPrompt payment terms: Net 30 Days\n"
        "--- PAGE 2 ---\n"
        + "\n".join(rows)
        + "\nAMENDMENT 7: increase the contract ceiling to $9 million.\n"
    )

    represented = build_compact_representation([SourceDocument("GSA schedule", source)])

    assert represented.adapters == ("contract-catalog-receipt-v1",)
    assert "Contract number: GS-TEST" in represented.text
    assert "catalog_rows=1100" in represented.text
    assert "catalog_rows_sha256=" in represented.text
    assert "PART-500" not in represented.text
    assert "AMENDMENT 7: increase the contract ceiling to $9 million." in represented.text


def test_aermod_receipt_keeps_authored_report_and_receipts_model_dump():
    headers = "\n".join(
        "*** AERMOD - VERSION 22112 ***" for _ in range(25)
    )
    records = "\n".join(
        f"LOCATION L{index:07d} VOLUME {index}.0 3750000.0 500.0"
        for index in range(600)
    )
    source = (
        "--- PAGE 1 ---\nAuthored air-quality finding: impacts are below threshold.\n"
        "--- PAGE 2 ---\nSO STARTING\n"
        + headers
        + "\n"
        + records
    )

    represented = build_compact_representation([SourceDocument("Air report", source)])

    assert represented.adapters == ("aermod-report-receipt-v1",)
    assert "Authored air-quality finding" in represented.text
    assert "LOCATION\t600" in represented.text
    assert "appendix_sha256=" in represented.text
    assert "LOCATION L0000500" not in represented.text


def test_aermod_receipt_reversibly_compacts_authored_fixed_width_tables():
    authored_rows = "\n".join(
        f"Finding {index:<8}      Receptor {index:<8}      {index}.000"
        for index in range(500)
    )
    headers = "\n".join(
        "*** AERMOD - VERSION 22112 ***" for _ in range(25)
    )
    records = "\n".join(
        f"LOCATION L{index:07d} VOLUME {index}.0 3750000.0 500.0"
        for index in range(600)
    )
    source = (
        "--- PAGE 1 ---\nAuthored findings table\n"
        + authored_rows
        + "\n--- PAGE 2 ---\nSO STARTING\n"
        + headers
        + "\n"
        + records
    )

    represented = build_compact_representation([SourceDocument("Air report", source)])

    assert represented.adapters == ("aermod-report-receipt-v1",)
    assert "adapter=aermod-authored-fixed-width-v1" in represented.text
    assert "Finding 0\tReceptor 0\t0.000" in represented.text
    assert "Finding 499\tReceptor 499\t499.000" in represented.text
    assert "authored_adapter=aermod-authored-fixed-width-v1" in represented.text


def test_election_receipt_keeps_countywide_and_audit_sections():
    detail_rows = []
    for index in range(1_667):
        detail_rows.extend(
            [
                f"[ADDED: Candidate {index}]",
                f"[ADDED: {index}]",
                "[ADDED: 0]",
                "[ADDED: 0]",
                "[ADDED: 0]",
                "[ADDED: 0]",
            ]
        )
    source = (
        "--- PAGE 1 ---\nOFFICIAL CANVASS\nCountywide Summary Results Report\n"
        "Winner: Example Candidate, 10,002 votes\n"
        "--- PAGE 63 ---\nPrecinct Results Report\n[ADDED: 001 TEST]\n"
        "Vote For 1\nCounty Office\n"
        + "\n".join(detail_rows)
        + "\n--- PAGE 2816 ---\nProvisional Ballot Report\n"
        "Ballot Audit Report: no discrepancy\n"
    )

    represented = build_compact_representation([SourceDocument("Canvass", source)])

    assert represented.adapters == ("election-canvass-receipt-v1",)
    assert "Winner: Example Candidate, 10,002 votes" in represented.text
    assert "Ballot Audit Report: no discrepancy" in represented.text
    assert "detail_sha256=" in represented.text
    assert "Candidate 1500" not in represented.text


def test_provider_blocked_public_evidence_becomes_auditable_receipt():
    document = SourceDocument(
        "Regarding Application_RubmapsReviews",
        ("rating review massage provider details\n" * 400),
        content_sha256="b" * 64,
        page_count=78,
    )

    assert needs_proactive_representation([document])
    represented = build_compact_representation([document])

    assert represented.adapters == ("public-record-evidence-receipt-v1",)
    assert "provider rejected the verbatim exhibit" in represented.text
    assert "text_sha256=" in represented.text
    assert "rating review massage provider details" not in represented.text
