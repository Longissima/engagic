"""Durable identifier extraction: instrument numbers and title codes.

Every case here came from a real title that misfired during the 2026-09-11
parity pass; the labelled-handle cases live in test_escribe_adapter.py.
"""

import pytest

from datetime import date

from database.id_generation import generate_matter_id
from parsing.identifiers import (
    Identifier,
    canonical_matter_file,
    detect_numbering_runs,
    extract_identifier,
    extract_leading_file_token,
    numbering_period,
)


class TestInstrumentNumbers:
    def test_resolution_and_ordinance_are_namespaced(self):
        assert extract_identifier("Resolution No. 2026-080 authorizing a HOME application")[:2] == (
            "Resolution 2026-080", "Resolution",
        )
        assert extract_identifier("Ordinance No. 2026-76 - Introduced by Council")[:2] == (
            "Ordinance 2026-76", "Ordinance",
        )

    def test_council_bill_forms(self):
        assert extract_identifier("Council Bill 2026-137 (Horton)")[0] == "Bill 2026-137"
        assert extract_identifier("First reading of Council Bill No. 10-26, an ordinance")[0] == "Bill 10-26"
        assert extract_identifier("Board Bill 107 Redevelopment plan")[0] == "Bill 107"

    def test_state_legislation_citation_is_not_a_city_bill(self):
        assert extract_identifier(
            "Annual Military Equipment Use Report 2025, per CA Assembly Bill No. 481"
        ) is None

    def test_amended_instrument_is_a_citation_not_the_item(self):
        assert extract_identifier(
            "Resolution No. _____ amending Resolution No. 1804 to authorize a lease"
        ) is None

    def test_body_citation_beyond_head_is_ignored(self):
        body = "x" * 400 + " as required by Ordinance No. 1187."
        assert extract_identifier("Discussion of pocket park concepts", body) is None

    def test_comma_thousands_ordinance(self):
        assert extract_identifier("Lease Agreement: ICRI", "adopt Ordinance No. 7,891-N.S. authorizing")[0] == (
            "Ordinance 7,891-N.S"
        )

    def test_bare_year_is_rejected(self):
        assert extract_identifier("Ordinance No. 2026") is None

    def test_labelled_handle_outranks_instrument(self):
        assert extract_identifier("Resolution No. 2026-14 approving Contract No. 12345 with Acme")[:2] == (
            "Contract 12345", "Contract",
        )


class TestFileNumberShapes:
    def test_case_file_with_dashed_suffix_is_whole(self):
        assert extract_identifier("CASE FILE NO. 2026-07-V")[:2] == ("File 2026-07-V", "File")
        assert extract_identifier("CASE FILE NO. 2025-90-P(ETJ)")[0] == "File 2025-90-P"

    def test_plain_file_number_still_keys(self):
        assert extract_identifier("File No. 15120 workers comp")[0] == "File 15120"


class TestTitleCodes:
    @pytest.mark.parametrize(
        "title,expected",
        [
            ("2026-469 Advisory Boards and Committee Reports", "2026-469"),
            ("22-1200-S49CD 13Motion relative to the reappointment", "22-1200-S49"),
            ("2026-05-19 Architectural Review Board Meeting Minutes", None),
            ("2027-28 Urban Forestry Work Plan", None),
            ("2026-2027 Operating Budget", None),
            ("2026-0615 Review", None),
            ("Approval of minutes 03-25", None),
        ],
    )
    def test_leading_file_token(self, title, expected):
        assert extract_leading_file_token(title) == expected

    def test_parenthesised_department_code(self):
        assert extract_identifier("(PC-11015) Application by Trinity Presbyterian Church to rezone")[:2] == (
            "PC-11015", None,
        )

    def test_dash_codes_keep_trailing_groups(self):
        assert extract_identifier("ZON-26-05-0014 - Zoning Change/Concept Plan")[0] == "ZON-26-05-0014"
        assert extract_identifier("CU-26-012 Conditional use permit for 12 Main St")[0] == "CU-26-012"

    def test_year_last_dash_code(self):
        assert extract_identifier("O-079-26 AN ORDINANCE AMENDING ORDINANCE NO. 102, SERIES 2016")[0] == "O-079-26"

    def test_date_like_committee_code_does_not_key(self):
        assert extract_identifier("CC-06-15 Committee report on parks") is None


class TestIdentityContract:
    """Cases from the 2026-10-01 over-merge audit (matters holding many
    unrelated items) and the St. Louis Board Bill 66 session collision."""

    @pytest.mark.parametrize("text", [
        "Item III.A - R-26-XXX- A RESOLUTION AUTHORIZING THE VILLAGE MANAGER",
        "Item III.B - Ordinance_No._26-_Non_Transport_Ambulance_Service_Charges.docx",
        "Assemblymember Steininger Amendment to Ord 2026-_____ (CBJ Code 69.40)",
        "A) Consideration of adopting Ordinance #O-26-___ increasing the Water rates",
        "Adopt Resolution No. 2026-XXX approving the appeal",
        "Adopt Resolution No. 2026-0XX approving the staff report",
        "Adopt Resolution No. XX-2026 approving the platform",
    ])
    def test_unassigned_number_is_not_identity(self, text):
        assert extract_identifier(text) is None

    @pytest.mark.parametrize("text", [
        "MEETING DISCLOSURES: devices shall be on mute during Board meetings pursuant to District Ordinance No. 12.",
        "written comments are made part of the record in accordance with Policy Resolution No. 10 (R2016-5).",
        "Annual Report and Recommendations in compliance with Ordinance #1868.",
        "An ordinance directing the Director of Streets to install speed humps pursuant to Ordinance Number 70333",
        "IH Borrower LP",
    ])
    def test_cited_authority_is_not_identity(self, text):
        body = "Section 2.2.1 (Zoning permit), from Ordinance no. 2926 of the City"
        assert extract_identifier(text, body if text == "IH Borrower LP" else None) is None

    def test_own_instrument_after_of_still_keys(self):
        assert extract_identifier("Adoption of Resolution No. 2026-14 approving the budget")[:2] == (
            "Resolution 2026-14", "Resolution")
        assert extract_identifier("Second Reading of Ordinance No. 1762, Land Development Code")[0] == "Ordinance 1762"

    def test_full_dashed_number_is_kept_whole(self):
        # CivicClerk's own parser keyed this as "RES2026", merging the year.
        assert extract_identifier("Consideration of Resolution Number 2026-059 related to Project Horizon")[0] == (
            "Resolution 2026-059")

    @pytest.mark.parametrize("text, year", [
        ("BILL NO. 106, 2026 AN ORDINANCE", "2026"),
        ("RESOLUTION NO. 100, 2026 AUTHORIZING", "2026"),
        ("Ordinance No. 198 of 2025 amending", "2025"),
        ("Ordinance No. 1 Series 2026", "2026"),
        ("Resolution No. 681 (2026) adopting", "2026"),
        ("Resolution No. 2026-14 approving the budget", None),
    ])
    def test_printed_year_is_captured_for_bare_numbers(self, text, year):
        assert extract_identifier(text).year == year

    def test_session_restart_splits_same_number(self):
        bill = extract_identifier("Board Bill Number 66 Introduced by Shane Cohn")
        assert bill == Identifier("Bill 66", "Bill")
        runs = [date(2026, 5, 4)]
        march = numbering_period(bill, date(2026, 3, 3), runs)
        september = numbering_period(bill, date(2026, 9, 9), runs, run_high=150)
        # The first run is unnamed, so the old session keeps its original id.
        assert (march, september) == (None, "2026")
        assert generate_matter_id("stlouisMO", matter_file=bill.file, matter_year=march) != generate_matter_id(
            "stlouisMO", matter_file=bill.file, matter_year=september)

    def test_carryover_past_the_new_runs_high_stays_in_the_old_run(self):
        runs = [date(2025, 4, 15), date(2026, 1, 5)]
        assert numbering_period(Identifier("Bill 170", "Bill"), date(2026, 1, 20), runs, run_high=6) == "2025"
        assert numbering_period(Identifier("Bill 7", "Bill"), date(2026, 1, 20), runs, run_high=6) == "2026"

    def test_period_only_where_needed(self):
        assert numbering_period(Identifier("Bill 66", "Bill"), date(2026, 9, 9)) is None
        assert numbering_period(Identifier("Bill 2026-5", "Bill"), date(2026, 9, 9), [date(2026, 5, 4)]) is None
        # A printed year beats everything: the government named the period.
        assert numbering_period(Identifier("Bill 65", "Bill", "2025"), date(2026, 1, 6), [date(2026, 1, 1)]) == "2025"

    def test_runs_are_detected_from_the_numbers_alone(self):
        def days(start, count, step=3):
            return [date.fromordinal(start.toordinal() + i * step) for i in range(count)]
        old_session = list(zip(days(date(2025, 4, 20), 175, 2), range(1, 176)))
        new_session = list(zip(days(date(2026, 5, 4), 60, 2), range(1, 61)))
        carryover = [(date(2026, 5, 6), 170), (date(2026, 5, 20), 168)]
        assert detect_numbering_runs(old_session + new_session + carryover) == [date(2026, 5, 4)]

    def test_cumulative_numbering_and_cited_small_numbers_are_not_resets(self):
        cumulative = [(date.fromordinal(date(2024, 1, 1).toordinal() + i * 4), 5800 + i) for i in range(160)]
        cited = [(date(2025, 3, 1), 5), (date(2025, 3, 8), 12), (date(2025, 4, 1), 3)]
        assert detect_numbering_runs(cumulative + cited) == []

    def test_year_free_ids_are_unchanged(self):
        assert generate_matter_id("nashvilleTN", matter_file="BL2025-1098", matter_year=None) == generate_matter_id(
            "nashvilleTN", matter_file="BL2025-1098")

    @pytest.mark.parametrize("raw, expected", [
        ("-----", None), ("RESOLUTION", None), ("Proclamation", None),
        ("Recommendation-to-go-into-Closure-at-5", None), ("2026-XX", None), ("Resolution 2026-XX", None),
        (" BL2025-1098 ", "BL2025-1098"), ("25-0583", "25-0583"), ("LU-2026-0023", "LU-2026-0023"),
        ("Contract 6006718-A1", "Contract 6006718-A1"),
    ])
    def test_supplied_matter_files_are_validated(self, raw, expected):
        assert canonical_matter_file(raw) == expected

    @pytest.mark.parametrize("text, expected", [
        ("Resolution 76-26, Approving a Contract with Tanner Industries", "Resolution 76-26"),
        ("Approve Resolution 022-2026, a resolution authorizing the Mayor", "Resolution 022-2026"),
        ("Adopt Ordinance 1719 on First Reading", "Ordinance 1719"),
        ("RESOLUTION 4302 - A Resolution Approving a Development Agreement", "Resolution 4302"),
        ("PROPOSED ORDINANCE NO. 6 - 26 - VACATION OF RIGHT OF WAY", "Ordinance 6-26"),
        ("A. ORDINANCE NO. 3.372.8 - A Bill For an Ordinance to Amend", "Ordinance 3.372.8"),
        ("Ordinance 2034 - Sewer Only Utility Rates", "Ordinance 2034"),
        ("Item Number 1 Board Bill Number 1Introduced by President Green", "Bill 1"),
    ])
    def test_unlabelled_and_irregular_instrument_numbers(self, text, expected):
        assert extract_identifier(text)[0] == expected

    @pytest.mark.parametrize("text", [
        "Special Meeting Attendance Reports (Portage County Ordinance 3.1.47 & 3.1.48)",
        "Report 0326001 and Zoning Amendatory Ordinance 03/0326001/26: Winnebago County",
        "Ordinance amending Chapter 5 of the municipal code",
    ])
    def test_code_sections_and_compound_files_are_not_instruments(self, text):
        assert extract_identifier(text) is None

    def test_own_instrument_closing_a_title_in_parentheses(self):
        assert extract_identifier("City Council Annual Meeting Schedule Amendment #3 (Resolution 2026-35)")[0] == (
            "Resolution 2026-35")

    @pytest.mark.parametrize("text, expected", [
        ("Ordinance No. 2026-10 - 2nd Quarter 2026 Budget Amendments", "Ordinance 2026-10"),
        ("FIRST AND ONLY READING OF ORDINANCE NO. 2026-0914 - 1 AN ORDINANCE", "Ordinance 2026-0914"),
    ])
    def test_spaced_dash_does_not_swallow_following_text(self, text, expected):
        assert extract_identifier(text)[0] == expected

    def test_citation_list_continuation_is_cited(self):
        assert extract_identifier(
            "System Development Charge rates in accordance with Ordinance No. 6161, Ordinance No. 6491 and Code"
        ) is None

    def test_list_position_and_associated_subject_are_not_identity(self):
        assert extract_identifier("Ordinance 1") is None
        assert extract_identifier("Ordinance No. 1 Series 2026")[0] == "Ordinance 1"
        assert extract_identifier(
            "RESOLUTION AUTHORIZING WAIVER OF THE 20-DAY ESTOPPEL PERIOD ASSOCIATED WITH ORDINANCE O-15-2026"
        ) is None

    def test_codified_by_is_a_citation(self):
        text = ("Item Number 1 Board Bill Number 66 Introduced by Shane Cohn An ordinance amending Chapter 3.160 "
                "of the Revised Code of the City of St. Louis, codified by Ordinance 71620, by adding a new section")
        assert extract_identifier(text)[0] == "Bill 66"

    @pytest.mark.parametrize("text, expected", [
        ("Consideration of Resolution No. 03-10-26-2. This item is the first reading", "Resolution 03-10-26-2"),
        ("Public HearingResolution26:021ADOPTION OF THE 2025 URBAN WATER MANAGEMENT PLAN", "Resolution 26:021"),
        ("Contract No. 210044- see attached.", "Contract 210044"),
    ])
    def test_long_and_colon_numbers(self, text, expected):
        assert extract_identifier(text)[0] == expected

    def test_year_prefix_with_dangling_dash_is_a_blank(self):
        assert extract_identifier("Approve the third amendment\nRESOLUTION NO. 26- \nRESOLUTION OF THE CITY") is None

    def test_abbreviated_labels(self):
        assert extract_identifier("GDC Amendment ORD 26-02")[0] == "Ordinance 26-02"
        assert extract_identifier("Approve Res. 2026-14 for paving")[0] == "Resolution 2026-14"
        assert extract_identifier("Residential 2026 permits report") is None

    @pytest.mark.parametrize("text, expected", [
        ("Substitute Bill No. 1 for Bill No. 133, 2026", Identifier("Bill 133", "Bill", "2026")),
        ("Bill No. 2 for Bill No. 182, 2025 unsigned and stating his objections",
         Identifier("Bill 182", "Bill", "2025")),
    ])
    def test_number_for_another_bill_defers_to_it(self, text, expected):
        assert extract_identifier(text) == expected

    def test_printed_sibling_names_the_period_when_no_runs(self):
        bill = Identifier("Bill 86", "Bill")
        assert numbering_period(bill, date(2026, 6, 30), printed_sibling="2026") == "2026"
        assert numbering_period(Identifier("Bill 86", "Bill", "2025"), date(2026, 6, 30), printed_sibling="2026") == "2025"
