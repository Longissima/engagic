"""Ranking candidate documents, and finding them when a portal names no field.

Every case is a real shape from the 2026-09-11 adapter audit.
"""

from bs4 import BeautifulSoup

from vendors.utils.documents import (
    DocumentCandidate,
    find_minutes_links,
    looks_like_minutes,
    pick_document,
    pick_document_url,
)


class TestRanking:
    def test_document_beats_the_viewer_that_renders_it(self):
        # BoardBook and Destiny both stored the viewer over an available PDF.
        chosen = pick_document_url([
            DocumentCandidate("https://x.org/Public/Minutes/706?meeting=1", "Minutes"),
            DocumentCandidate("https://x.org/files/minutes.pdf", "Minutes"),
        ])
        assert chosen == "https://x.org/files/minutes.pdf"

    def test_pdf_beats_html_and_docx_when_the_vendor_declares_the_format(self):
        # PrimeGov URLs carry no extension; compileOutputType is the format.
        chosen = pick_document_url([
            DocumentCandidate("https://x.org/CompiledDocument?id=1&cot=3", "Minutes", "html"),
            DocumentCandidate("https://x.org/CompiledDocument?id=1&cot=2", "Minutes", "docx"),
            DocumentCandidate("https://x.org/CompiledDocument?id=1&cot=1", "Minutes", "pdf"),
        ])
        assert chosen is not None and chosen.endswith("cot=1")

    def test_approved_beats_draft(self):
        chosen = pick_document_url([
            DocumentCandidate("https://x.org/d.pdf", "Draft Minutes"),
            DocumentCandidate("https://x.org/a.pdf", "Approved Minutes"),
        ])
        assert chosen == "https://x.org/a.pdf"

    def test_a_draft_is_still_taken_when_it_is_all_there_is(self):
        assert pick_document_url([DocumentCandidate("https://x.org/d.pdf", "Draft Minutes")])

    def test_open_session_beats_closed_even_when_closed_comes_first(self):
        # Ross stored march_12_2026_closed_session.pdf because DOM order won.
        chosen = pick_document_url([
            DocumentCandidate("https://x.org/march_12_2026_closed_session.pdf", "Minutes"),
            DocumentCandidate("https://x.org/march_12_2026_minutes.pdf", "Minutes"),
        ])
        assert chosen is not None and chosen.endswith("march_12_2026_minutes.pdf")

    def test_closed_session_is_reachable_when_asked_for(self):
        chosen = pick_document_url(
            [
                DocumentCandidate("https://x.org/regular.pdf", "Minutes"),
                DocumentCandidate("https://x.org/closed.pdf", "Closed Session Minutes"),
            ],
            prefer_closed=True,
        )
        assert chosen == "https://x.org/closed.pdf"

    def test_session_outranks_format(self):
        """A closed-session PDF must not beat the open-session HTML."""
        chosen = pick_document_url([
            DocumentCandidate("https://x.org/closed.pdf", "Executive Session Minutes"),
            DocumentCandidate("https://x.org/open.html", "Minutes"),
        ])
        assert chosen == "https://x.org/open.html"

    def test_ties_keep_the_portal_order(self):
        first = DocumentCandidate("https://x.org/1.pdf", "Minutes")
        second = DocumentCandidate("https://x.org/2.pdf", "Minutes")
        assert pick_document([first, second]) is first

    def test_no_candidates(self):
        assert pick_document([]) is None
        assert pick_document_url([DocumentCandidate("", "Minutes")]) is None


class TestLooksLikeMinutes:
    def test_accepts_local_vocabulary(self):
        assert looks_like_minutes("Legal Minutes")
        assert looks_like_minutes("Journal of Proceedings")
        assert looks_like_minutes("ctl00_hypMinutesPDF")

    def test_rejects_things_that_merely_mention_minutes(self):
        assert not looks_like_minutes("Agenda")
        assert not looks_like_minutes("Approval of the Minutes")
        assert not looks_like_minutes("15 minutes")
        assert not looks_like_minutes("")


class TestFindMinutesLinks:
    def test_reads_the_word_from_an_icon_alt(self):
        # NovusAgenda's minutes anchor wraps an image and has no link text.
        row = BeautifulSoup(
            '<tr><td><a href="DisplayAgendaPDF.ashx?MeetingID=652">Regular Meeting</a></td>'
            '<td><a href="DisplayAgendaPDF.ashx?MinutesMeetingID=700">'
            '<img alt="Legal Minutes"/></a></td></tr>',
            "html.parser",
        )
        found = find_minutes_links(row, "https://plano.novusagenda.com/agendapublic/")
        assert [c.url for c in found] == [
            "https://plano.novusagenda.com/agendapublic/DisplayAgendaPDF.ashx?MinutesMeetingID=700"
        ]

    def test_skips_the_agenda_in_the_same_row(self):
        row = BeautifulSoup(
            '<tr><td><a href="/agenda.pdf">Agenda</a></td>'
            '<td><a href="/minutes.pdf">Minutes</a></td></tr>',
            "html.parser",
        )
        assert [c.url for c in find_minutes_links(row)] == ["/minutes.pdf"]

    def test_ignores_anchors_that_go_nowhere(self):
        row = BeautifulSoup(
            '<tr><td><a href="#">Minutes</a><a href="javascript:void(0)">Minutes</a></td></tr>',
            "html.parser",
        )
        assert find_minutes_links(row) == []

    def test_ranks_what_it_finds(self):
        row = BeautifulSoup(
            '<tr><td><a href="/MinutesViewer.php?clip_id=3">View Minutes</a>'
            '<a href="/docs/minutes.pdf">Minutes</a></td></tr>',
            "html.parser",
        )
        assert pick_document_url(find_minutes_links(row)) == "/docs/minutes.pdf"
