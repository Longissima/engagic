"""Generic minutes engine, on snippets lifted from the 2026-09-11 reservoir."""

from parsing.rollcall.attendance import parse_attendance
from parsing.rollcall.align import anchor_items
from parsing.rollcall.engine import Gazetteer, parse_meeting
from parsing.rollcall.evidence import find_evidence


ALBUQUERQUE = """
      Attendance:
                        Present  5 -  Dan Lewis, Joaquín Baca, Dan Champine, Klarissa Peña, and Stephanie
                               W. Telles
a.    EC-26-176        Establishing Residential Parking Permits - 400 Hannett Avenue
                    A motion was made by Councilor Lewis that this matter be Sent to Council with
                          a recommendation of Be Approved. The motion carried by the following vote:
                              For:  5 -  Lewis, Baca, Champine, Peña, and Telles
b.    EC-26-202         Notification of a grant application submitted by the Aviation
                    A motion was made by Councilor Lewis that this matter be Overridden. The motion failed by the following vote:
                              For:  2 -  Lewis, Baca
                         Against:  3 -  Champine, Peña, and Telles
"""

ALAMEDA = """
   Action
    Supervisor Tam motioned, and Supervisor Miley seconded, to recommend approval of the Draft
    Ordinance Prohibiting the Sale of Nitrous Oxide and Nitrous Oxide Devices.
    Ayes: Supervisor Tam, Supervisor Miley – Two (2)
    Noes:
   Excused:
    Abstained:
   Motion passed 2/0
"""

ALPHARETTA = """
         1.  DRB260012 Jiffy Lube 207 South Main St Review signage
     Board Member Rowen offered a motion to approve.
                Board Member Owens seconded the motion.
                Motion carried (6-0).
"""
ALPHARETTA_ITEM = [{
    "id": "i1", "sequence": 1, "agenda_number": "1.", "matter_file": None,
    "matter_id": "m1", "title": "DRB260012 Jiffy Lube 207 South Main St Review signage",
}]


class TestAttendance:
    def test_label_with_count_wrapping_lines(self):
        att = parse_attendance(ALBUQUERQUE)
        assert att.present == ["Dan Lewis", "Joaquín Baca", "Dan Champine", "Klarissa Peña", "Stephanie W. Telles"]
        assert att.present_count == 5

    def test_narrative_sentence_across_lines(self):
        text = (
            "Mayor Moriwaki, Deputy Mayor Hytopoulos and Councilmembers Lant, Mathews,\n"
            "Nelson, and Schneider were present. Councilmember Fantroy-Johnson was absent\nand excused."
        )
        att = parse_attendance(text)
        assert att.present == ["Moriwaki", "Hytopoulos", "Lant", "Mathews", "Nelson", "Schneider"]
        assert att.absent == ["Fantroy-Johnson"]


    def test_names_after_present_colon_and_inline_absent(self):
        text = (
            "3.   ROLL CALL\nThe following City Council Members were present: Jack Sheard, Mark Stelk, Jason Conley,\n"
            "Mitchell Nickerson and Doug\nLanfear. Absent: Mike Paulick.\n4. SUBMITTAL"
        )
        att = parse_attendance(text)
        assert att.present == ["Jack Sheard", "Mark Stelk", "Jason Conley", "Mitchell Nickerson", "Doug Lanfear"]
        assert att.absent == ["Mike Paulick"]

    def test_in_attendance_included(self):
        text = "The meeting was called to order at 3:29 p.m. Board members in attendance included Andrew Reynolds, Michael Telich,\n Michael Carmouche and Brian LaFleur. Also, in attendance were Deane Frazier."
        att = parse_attendance(text)
        assert att.present == ["Andrew Reynolds", "Michael Telich", "Michael Carmouche", "Brian LaFleur"]


    def test_present_line_running_into_absent_and_guests(self):
        att = parse_attendance("Members Present: Eric Gilbertson (Chair), Robert McCullough (Vice Chair), Paul Carnahan, Yana Walder. Absent: Bryan Jones & Linden Chozinska Guests attending: Sebastian Delgado, David Schutz. Staff: Meredith Crandall.")
        assert att.present == ["Eric Gilbertson", "Robert McCullough", "Paul Carnahan", "Yana Walder"]
        assert att.absent == ["Bryan Jones", "Linden Chozinska"]

    def test_staff_tokens_dropped_from_narrative_roster(self):
        att = parse_attendance("Present were Alice Sweetland, CCS Manager, LaRita Montgomery, Recording Secretaries, Sandra Diaz.")
        assert att.present == ["Alice Sweetland", "LaRita Montgomery", "Sandra Diaz"]


class TestEvidence:
    def test_for_against_lists_with_counts(self):
        block = ALBUQUERQUE.split("b.    EC-26-202")[1]
        ev = find_evidence(block)
        assert len(ev) == 1
        assert ev[0].outcome == "FAIL"
        assert [(s.value, s.names, s.stated) for s in ev[0].sections] == [
            ("AYE", ["Lewis", "Baca"], 2),
            ("NO", ["Champine", "Peña", "Telles"], 3),
        ]

    def test_lists_before_result_with_spelled_count(self):
        ev = find_evidence(ALAMEDA)
        assert ev[-1].outcome == "PASS"
        assert ev[-1].tally == (2, 0, 0)
        assert [(s.value, s.names) for s in ev[-1].sections if s.names] == [("AYE", ["Tam", "Miley"])]

    def test_to_separator_and_abstention_mention(self):
        assert find_evidence("RESULT: ADOPTED [12 TO 0]\nAYES: A, B")[0].tally == (12, 0, 0)
        ev = find_evidence("Motion Passed 5-0 with one abstention\nCommissioner Rafel abstained.")[0]
        assert ev.tally == (5, 0, 1) and not ev.unanimous

    def test_recorded_dissent_label_is_read(self):
        ev = find_evidence("RESULT: Approved\nAYES: Mayr, Bratt, Nerbun\nDEEMED NAY: Bach\n")[0]
        assert ("NO", ["Bach"]) in [(s.value, s.names) for s in ev.sections]

    def test_bare_disposition_line_closes_a_named_block(self):
        block = (
            "Dr. Michael Aiello motioned, seconded by Julie Matuzak to approve the Consent Agenda.\n"
            "Ayes: Dr. Michael Aiello, Julie Matuzak, Bruce Wade\n"
            "Nays: None\n"
            "Abstain: None\n"
            "Absent: Shannon King\n"
            "Passed\n"
        )
        ev = find_evidence(block)[-1]
        assert ev.outcome == "PASS"
        assert ("AYE", ["Michael Aiello", "Julie Matuzak", "Bruce Wade"]) in [
            (s.value, s.names) for s in ev.sections
        ]

    def test_tally_detached_from_the_word_motion(self):
        ev = find_evidence("Motion/second to approve the Agenda by Commissioners Fuller/McCord carried 6-0.\n")[0]
        assert ev.outcome == "PASS" and ev.tally == (6, 0, 0)

    def test_tally_only(self):
        ev = find_evidence(ALPHARETTA)
        assert ev[-1].outcome == "PASS" and ev[-1].tally == (6, 0, 0) and not ev[-1].named


    def test_prose_unanimous_is_not_a_vote(self):
        assert find_evidence("provided justification of the unanimous request from the Main Street Board received on June 10.") == []
        assert find_evidence("On roll call vote, motion carried unanimously.")[0].unanimous

    def test_bracketed_tally_is_read(self):
        ev = find_evidence("RESULT: PASSED [9-0]\nMOVER: A\nAYES: Bruce Bondy, Megan Paul")
        assert ev[0].tally == (9, 0, 0)

    def test_staff_line_is_not_attendance(self):
        att = parse_attendance("Staff present: Park, Recreation, MAPD Intern\nThe following were present: Park, Recreation, Intern.")
        assert att.present == []


    def test_sentence_debris_is_not_a_name(self):
        from parsing.rollcall.names import split_names
        assert split_names("Hytopoulos, Lant, for the vote, _ Robichaux") == ["Hytopoulos", "Lant", "Robichaux"]


class TestGazetteer:
    def test_accents_and_middle_initials_do_not_split_a_member(self):
        gz = Gazetteer(["Klarissa J. Peña", "Klarissa Pena", "Joaquin Baca", "Joaquín Baca"])
        assert gz.resolve("Peña") == "Klarissa J. Peña"
        assert gz.resolve("Councilor Baca") == "Joaquin Baca"

    def test_shared_surname_is_ambiguous(self):
        gz = Gazetteer(["Dan Lewis", "Shontel Lewis"])
        assert gz.resolve("Lewis") is None
        assert gz.resolve("Dan Lewis") == "Dan Lewis"


class TestEngine:
    ITEMS = [
        {"id": "i1", "sequence": 2, "agenda_number": "a.", "matter_file": "EC-26-176", "matter_id": "m1", "title": "Establishing Residential Parking Permits"},
        {"id": "i2", "sequence": 3, "agenda_number": "b.", "matter_file": "EC-26-202", "matter_id": "m2", "title": "Notification of a grant application"},
    ]

    def test_named_votes_from_attendance_roster_only(self):
        parsed = parse_meeting(ALBUQUERQUE, self.ITEMS, roster=[])
        assert parsed.items_anchored == 2
        assert [p.method for p in parsed.published] == ["named", "named"]
        first, second = parsed.published
        assert first.outcome == "PASS" and len(first.member_votes) == 5
        assert second.outcome == "FAIL"
        assert dict(second.member_votes)["Klarissa Peña"] == "NO"
        assert second.tally["yes"] == 2 and second.tally["no"] == 3

    def test_unanimous_tally_attributes_against_attendance(self):
        text = "Present: Rowen, Owens, Lee\n" + ALPHARETTA.replace("(6-0)", "(3-0)")
        parsed = parse_meeting(text, ALPHARETTA_ITEM, roster=[])
        assert parsed.published and parsed.published[0].method == "unanimous"
        assert sorted(parsed.published[0].member_votes) == [("Lee", "AYE"), ("Owens", "AYE"), ("Rowen", "AYE")]

    def test_split_tally_without_names_is_outcome_only(self):
        text = "Present: Rowen, Owens, Lee, Kim\n" + ALPHARETTA.replace("(6-0)", "(3-1)")
        parsed = parse_meeting(text, ALPHARETTA_ITEM, roster=[])
        assert parsed.published[0].method == "tally" and parsed.published[0].member_votes == []

    def test_named_lists_seed_roster_when_document_is_the_only_source(self):
        text = ALBUQUERQUE.split("a.    EC-26-176")[0].split("Attendance:")[0] + "a.    EC-26-176" + ALBUQUERQUE.split("a.    EC-26-176")[1]
        parsed = parse_meeting(text, self.ITEMS, roster=[])
        assert parsed.roster_source == "named_lists"
        assert [p.method for p in parsed.published] == ["named", "named"]

    def test_bare_surname_merges_into_full_name(self):
        gz = Gazetteer(["Claudia Balducci", "Balducci", "Reagan Dunn"])
        assert gz.resolve("Balducci") == "Claudia Balducci"
        assert len(gz.canonical) == 2

    def test_unresolvable_name_abstains(self):
        head, tail = ALBUQUERQUE.split("a.    EC-26-176")
        text = head + "a.    EC-26-176" + tail.replace("Telles", "Nobody")
        parsed = parse_meeting(text, self.ITEMS, roster=[])
        assert parsed.published == []
        assert any("unresolved" in r for ab in parsed.abstained for r in ab.reasons)


class TestGuards:
    ITEM = [{"id": "i1", "sequence": 1, "agenda_number": "1.", "matter_file": None, "matter_id": "m1", "title": "Final Site Plan for Lighthouse Pentecostal at 590 Fort Smith"}]

    def test_mover_from_another_body_abstains(self):
        text = (
            "Present: Angel Ortiz, Bobbie Degon, Brandon Hatch, Daryl Cooley\n"
            "1. Final Site Plan for Lighthouse Pentecostal at 590 Fort Smith\n"
            "Motion by Vice-chair Wallace, seconded by member Cox, to approve the Final Site Plan.\n"
            "The motion carried unanimously.\n"
        )
        parsed = parse_meeting(text, self.ITEM, roster=[])
        assert parsed.published == []
        assert any("membership" in r for ab in parsed.abstained for r in ab.reasons)

    def test_mover_on_the_roster_publishes(self):
        text = (
            "Present: Angel Ortiz, Bobbie Degon, Brandon Hatch, Daryl Cooley\n"
            "1. Final Site Plan for Lighthouse Pentecostal at 590 Fort Smith\n"
            "Motion by Ortiz, seconded by Degon, to approve the Final Site Plan.\n"
            "The motion carried unanimously.\n"
        )
        parsed = parse_meeting(text, self.ITEM, roster=[])
        assert [p.method for p in parsed.published] == ["unanimous"]

    def test_adjournment_motion_is_not_an_item_vote(self):
        text = (
            "Present: Ortiz, Degon, Hatch\n"
            "1. Final Site Plan for Lighthouse Pentecostal at 590 Fort Smith\n"
            "It was moved by Ortiz and supported by Degon to adjourn.\n"
            "Adopted by the following vote: unanimous.\n"
        )
        parsed = parse_meeting(text, self.ITEM, roster=[])
        assert parsed.published == [] and parsed.procedural_skipped == 1

    def test_section_heading_item_is_skipped(self):
        items = [{"id": "i1", "sequence": 1, "agenda_number": "5.", "matter_file": None, "matter_id": "m1", "title": "OLD BUSINESS:"}]
        text = "Present: Ortiz, Degon\n5. OLD BUSINESS:\nMotion by Ortiz, seconded by Degon. The motion carried unanimously.\n"
        parsed = parse_meeting(text, items, roster=[])
        assert parsed.published == [] and parsed.procedural_skipped == 1

    def test_mover_name_stops_at_the_sentence(self):
        from parsing.rollcall.names import clean_name
        assert clean_name("Ron Leino. Motion passed unanimously") == "Ron Leino"
        assert clean_name("Darla LeClair. The motion carried") == "Darla LeClair"

    def test_honorific_period_is_not_a_sentence_break(self):
        from parsing.rollcall.names import clean_name
        assert clean_name("Dr. Michael Aiello") == "Michael Aiello"
        assert clean_name("Mr. J. Smith") == "J. Smith"

    def test_lowercase_office_word_inside_a_mover_name(self):
        from parsing.rollcall.evidence import find_evidence
        ev = find_evidence("A motion offered by Council member Lewis, duly seconded by Council member Hinds, carried by the following vote:\nAye: 2 - Lewis, Hinds\n")[0]
        assert any("Lewis" in m for m in ev.movers)

    def test_seconder_suffix_form_counts_as_a_mover(self):
        from parsing.rollcall.evidence import find_evidence
        ev = find_evidence("Ordinance 26-24 presented. Councilmember Morris seconded the motion. The motion passed 5-0.\n")[0]
        assert any("Morris" in m for m in ev.movers)

    def test_no_roster_means_no_membership_check(self):
        text = "1. Site Plan approval for the corner parcel\nMotion by Commissioner Forest, second by Commissioner deJong. Motion carried 5-0.\n"
        items = [{"id": "i1", "sequence": 1, "agenda_number": "1.", "matter_file": None, "matter_id": "m1", "title": "Site Plan approval for the corner parcel"}]
        parsed = parse_meeting(text, items, roster=[])
        assert [p.method for p in parsed.published] == ["tally"]

    def test_minutes_approval_items_are_procedure(self):
        items = [{"id": "i1", "sequence": 1, "agenda_number": "8.", "matter_file": None, "matter_id": "m1", "title": "June 23, 2026 - Policy Meeting minutes"}]
        text = "Present: Wurth, Ratchford\n8. June 23, 2026 - Policy Meeting minutes\nWurth moved, seconded by Ratchford. Motion carried 2-0.\n"
        parsed = parse_meeting(text, items, roster=[])
        assert parsed.published == [] and parsed.procedural_skipped == 1

    def test_trailing_role_word_is_stripped(self):
        from parsing.rollcall.names import clean_name
        assert clean_name("Aaron Van Krey Members") == "Aaron Van Krey"

    def test_committee_member_title_is_stripped(self):
        from parsing.rollcall.names import clean_name
        assert clean_name("Committee Member James Liggins") == "James Liggins"
        assert clean_name("Vice-chair Wallace") == "Wallace"


class TestAlign:
    def test_procedural_heading_closes_block(self):
        from parsing.rollcall.align import blocks
        text = "1. Personnel Update\nDiscussion.\n9. RECONVENE TO OPEN SESSION\nMotion Passed 4-0\n"
        items = [{"id": "x", "sequence": 1, "agenda_number": "1.", "matter_file": None, "matter_id": "m", "title": "Personnel Update - Fire Department"}]
        anchors = anchor_items(text, items)
        assert "Motion Passed" not in text[anchors[0].start:blocks(text, anchors)[0]["end"]]

    def test_caps_section_heading_closes_block(self):
        from parsing.rollcall.align import blocks
        text = "SUB23-00020 Callisto Heights Subdivision\nDiscussion of the subdivision.\nVII. ORDINANCES\nRESULT: APPROVED BY UNANIMOUS CONSENT\n"
        items = [{"id": "x", "sequence": 1, "agenda_number": None, "matter_file": "SUB23-00020", "matter_id": "m", "title": "Callisto Heights Subdivision"}]
        anchors = anchor_items(text, items)
        assert "UNANIMOUS CONSENT" not in text[anchors[0].start:blocks(text, anchors)[0]["end"]]

    def test_last_block_stops_at_adjournment(self):
        from parsing.rollcall.align import blocks
        text = "1. Budget\nMotion carried (5-0).\n5. ADJOURN\nMotion carried (5-0).\n"
        items = [{"id": "x", "sequence": 1, "agenda_number": "1.", "matter_file": None, "matter_id": "m", "title": "Budget review of the year"}]
        anchors = anchor_items(text, items)
        assert anchors and text[blocks(text, anchors)[0]["end"]:].startswith("5. ADJOURN")

    def test_page_marker_does_not_close_block(self):
        """Powell OH, 2026-08-04: our own extractor stamps the page separator,
        and reading it as a heading stranded the motion on the next page."""
        from parsing.rollcall.align import blocks
        text = (
            "RESOLUTION 2026-39\n"
            "Mayor Karr read the resolution and council discussed it.\n"
            "--- PAGE 8 ---\n"
            "MOTION: Vice-Mayor Tom Counts moved to approve Resolution 2026-39. "
            "Councilmember David Lester seconded. Motion passed.\n"
        )
        items = [{"id": "x", "sequence": 1, "agenda_number": None,
                  "matter_file": "RESOLUTION 2026-39", "matter_id": "m",
                  "title": "Extend professional services agreement"}]
        anchors = anchor_items(text, items)
        block = blocks(text, anchors)[0]
        assert "Motion passed" in text[block["start"]:block["end"]]

    def test_vote_label_heading_does_not_close_block(self):
        """A caps line that announces the vote is scaffolding, not a section."""
        from parsing.rollcall.align import blocks
        text = (
            "1. Budget Amendment\n"
            "The finance director presented the amendment.\n"
            "D. MOTION\n"
            "Motion carried 5-0.\n"
        )
        items = [{"id": "x", "sequence": 1, "agenda_number": "1.", "matter_file": None,
                  "matter_id": "m", "title": "Budget Amendment for the fiscal year"}]
        anchors = anchor_items(text, items)
        block = blocks(text, anchors)[0]
        assert "Motion carried" in text[block["start"]:block["end"]]

    def test_running_header_does_not_close_block(self):
        """Letterhead reprinted on every page is furniture; a real heading
        appears once and still closes the block."""
        from parsing.rollcall.align import blocks
        text = (
            "CITY OF MARLBOROUGH\n"
            "1. Zoning Petition Review\n"
            "The board heard the petition.\n"
            "CITY OF MARLBOROUGH\n"
            "Motion carried 7-0.\n"
            "CITY OF MARLBOROUGH\n"
        )
        items = [{"id": "x", "sequence": 1, "agenda_number": "1.", "matter_file": None,
                  "matter_id": "m", "title": "Zoning Petition Review for the parcel"}]
        anchors = anchor_items(text, items)
        block = blocks(text, anchors)[0]
        assert "Motion carried" in text[block["start"]:block["end"]]

    def test_measurement_numbers_are_not_agenda_anchors(self):
        text = "43.7 %, where 30% is the maximum\nVote: 3-0-0"
        items = [{"id": "x", "sequence": 1, "agenda_number": "43.7", "matter_file": None, "matter_id": "m", "title": "%, where 30% is the maximum amount"}]
        assert anchor_items(text, items) == [] or anchor_items(text, items)[0].rung != "agenda_number"
