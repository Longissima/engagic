"""eScribe adapter: body-text extraction and body-carried matter identifiers.

Every HTML shape here was reduced from a real merged agenda in the fixture
corpus (28 pages, 14 cities). The city named in each test is the instance that
produces that shape in production.
"""

import asyncio

import pytest
from bs4 import BeautifulSoup

from vendors.adapters.escribe_adapter_async import AsyncEscribeAdapter
from pipeline.utils import MatterWorkSnapshot, normalize_body_text
from parsing.identifiers import extract_identifier


def parse(html: str):
    adapter = AsyncEscribeAdapter("pub-test")
    soup = BeautifulSoup(html, "html.parser")
    return asyncio.run(adapter._parse_agenda_items(soup, "uuid", "https://pub-test.escribemeetings.com/"))


def container(inner: str, item_id: int = 80) -> str:
    return f"""
    <div class="AgendaItemContainer">
      <div class="AgendaItem AgendaItem{item_id}">
        {inner}
      </div>
    </div>
    """


def title_row(number: str, title: str, item_id: int = 80) -> str:
    return f"""
    <div class="AgendaItemTitleRow">
      <h3><div class="AgendaItemCounter">{number}</div>
        <div class="AgendaItemNavigate">
          <div class="AgendaItemTitle"><a href="javascript:SelectItem({item_id});">{title}</a></div>
        </div>
      </h3>
    </div>
    """


class TestBodyExtraction:
    def test_department_banner_does_not_replace_body(self):
        """Detroit: banner in content row 1, real body in content row 2.

        Taking the first content row returned the banner and dropped the body on
        roughly a third of Detroit's items.
        """
        html = container(
            '<div class="AgendaItemContentRow"><div class="AgendaItemHeader">'
            "OFFICE OF CONTRACTING AND PROCUREMENT</div></div>"
            + title_row("17.1", "Whitfield-Calloway, reso. autho.")
            + '<div class="AgendaItemContentRow"><div class="AgendaItemDescription RichText">'
            "<p><strong>Contract No. 6007968 -</strong> 100% City Funding to provide a digital "
            "evidence management solution. Contractor: Motorola Solutions Inc.</p></div></div>"
        )
        item = parse(html)[0]

        assert "digital evidence management solution" in item["body_text"]
        assert item["body_text"] != "OFFICE OF CONTRACTING AND PROCUREMENT"
        # The banner is real context for a boilerplate title, kept as a prefix.
        assert item["body_text"].startswith("OFFICE OF CONTRACTING AND PROCUREMENT")

    def test_motion_text_is_the_body_when_no_description(self):
        """Richmond publishes 45 of 48 items as MotionText with no description."""
        html = container(
            title_row("I.1", "Legal Services Agreement Amendment No. 2")
            + '<div class="AgendaItemContentRow"><ul class="AgendaItemMotions">'
            '<li class="AgendaItemMotion"><div class="Number"></div>'
            '<div class="MotionLabel">Recommended Action:</div>'
            '<div class="MotionText">APPROVE a second amendment to the legal services '
            "agreement to increase the payment limit by $75,000.</div></li></ul></div>"
        )
        item = parse(html)[0]

        assert item["body_text"].startswith("APPROVE a second amendment")
        assert "Recommended Action" not in item["body_text"]

    def test_placeholder_badge_alone_yields_no_body(self):
        """Orlando emits a routing badge as the entire description on 84 of 98 items."""
        for placeholder in ("District: ALL", "District : 1, 3, 5", "No agenda items"):
            html = container(
                title_row("3", "Consent Agenda")
                + '<div class="AgendaItemContentRow"><div class="AgendaItemDescription">'
                f"{placeholder}</div></div>"
            )
            assert parse(html)[0]["body_text"] == "", placeholder

    def test_placeholder_badge_prefixing_real_text_is_kept(self):
        """The same badge legitimately prefixes kilobytes of real text."""
        html = container(
            title_row("4", "Special Event Permits")
            + '<div class="AgendaItemContentRow"><div class="AgendaItemDescription">'
            "<p>District: 1, 3, 4, 5, 6</p><p>ID: 4700 Wall Street Plaza Rolling 18B, "
            "alcohol and amplified sound between 4:00pm and 12:00am.</p></div></div>"
        )
        item = parse(html)[0]

        assert "Wall Street Plaza" in item["body_text"]
        # Inline spans must not be split: a " " separator turns "ID:" into "I D:".
        assert "ID: 4700" in item["body_text"]

    def test_word_pasted_glyph_spans_are_not_shredded(self):
        """Chula Vista pastes from Word, one span per glyph cluster."""
        html = container(
            title_row("1", "ROLL CALL")
            + '<div class="AgendaItemContentRow"><div class="AgendaItemDescription">'
            '<span class="TextRun"><span class="NormalTextRun">Co</span>'
            '<span class="NormalTextRun">mmi</span><span class="NormalTextRun">ss</span>'
            '<span class="NormalTextRun">ioners</span></span> Alatorre, Knox, and Chair Korgan'
            "</div></div>"
        )
        assert parse(html)[0]["body_text"] == "Commissioners Alatorre, Knox, and Chair Korgan"

    def test_block_boundaries_do_not_fuse_words(self):
        """Bakersfield: sibling <p> blocks fuse without a boundary separator."""
        html = container(
            title_row("8", "Director's Report")
            + '<div class="AgendaItemContentRow"><div class="AgendaItemDescription">'
            "<p>Street Tier = 2; Park Tier = 1.5 - Ward 5</p>"
            "<p>ROI No. 2264 adding Area 4-308</p></div></div>"
        )
        assert "Ward 5ROI" not in parse(html)[0]["body_text"]

    def test_attachment_names_never_leak_into_body(self):
        html = container(
            title_row("2", "Consent")
            + '<div class="AgendaItemContentRow">'
            '<div class="AgendaItemAttachmentsList"><div class="AgendaItemAttachment">'
            '<a href="filestream.ashx?DocumentId=272997">Contract 6001723-A5.pdf</a>'
            "</div></div></div>"
        )
        item = parse(html)[0]

        assert item["body_text"] == ""
        assert item["attachments"][0]["name"] == "Contract 6001723-A5.pdf"

    def test_closed_session_item_is_captured(self):
        """Richmond closed-session items have no AgendaItemNNN class and no SelectItem link."""
        html = """
        <div class="AgendaItemContainer">
          <div class="AgendaItem">
            <div class="ClosedAgendaItemTitleRow">
              <h3><div class="ClosedAgendaItemCounter">C.1</div>
                <div class="ClosedAgendaItemTitle">LIABILITY CLAIMS</div>
              </h3>
            </div>
            <div class="AgendaItemContentRow">
              <div class="AgendaItemAttachmentsList AgendaItemPublicCommentListIndent3Closed"></div>
              <div class="AgendaItemDescription">Claimant: Manwell Gali. Agency Against: City of Richmond</div>
            </div>
          </div>
        </div>
        """
        items = parse(html)

        assert len(items) == 1
        assert items[0]["vendor_item_id"] == "3"
        assert items[0]["title"] == "LIABILITY CLAIMS"
        assert items[0]["agenda_number"] == "C.1"
        assert "Manwell Gali" in items[0]["body_text"]

    def test_parent_container_does_not_steal_child_body(self):
        """A parent is skipped as an item, and must never absorb a child's text."""
        html = f"""
        <div class="AgendaItemContainer">
          <div class="AgendaItem AgendaItem17">{title_row("17.", "INTERNAL OPERATIONS", 17)}</div>
          <div>{container(title_row("17.1", "Whitfield-Calloway, reso. autho.", 58)
                          + '<div class="AgendaItemContentRow"><div class="AgendaItemDescription">'
                            "Contract No. 6007968 for body-worn cameras.</div></div>", 58)}</div>
        </div>
        """
        items = parse(html)

        assert len(items) == 1
        assert items[0]["agenda_number"] == "17.1"


class TestBodyIdentifiers:
    """Identifiers cited in agenda text, extracted for every vendor in the sync funnel."""

    def test_contract_number_becomes_matter_file(self):
        assert extract_identifier(
            "Whitfield-Calloway, reso. autho.",
            "Contract No. 6007968 - 100% City Funding to provide a digital evidence "
            "management solution. Contractor: Motorola Solutions Inc.",
        ) == ("Contract 6007968", "Contract")

    def test_amendment_suffix_is_preserved(self):
        """6006718-A1 is a distinct council action from 6006718."""
        original = extract_identifier("Contract No. 6006718 - to purchase network equipment.")
        amendment = extract_identifier("Contract No. 6006718-A1 - AMEND 1 - increase of funds.")

        assert original[0] == "Contract 6006718"
        assert amendment[0] == "Contract 6006718-A1"

    def test_law_department_file_outranks_court_case(self):
        """Settlements cite both; the city's own file number is the stable handle."""
        assert extract_identifier(
            "Settlement in lawsuit of Michael Butts v City of Detroit; "
            "Case No. 25-011182 NI, File No. L25-8029, (RJB) A37000 in the amount of $76,000.00."
        ) == ("File L25-8029", "File")

    def test_unlabelled_law_file_keys_the_same_as_a_labelled_one(self):
        """One agenda writes 'File No. L24-01403', another drops the label."""
        labelled = extract_identifier("Settlement; Case No. 24-016365-NF, File No. L24-01403 (VI).")
        unlabelled = extract_identifier("Settlement; Case No. 24-016365-NF; L24-01403 (VI).")

        assert labelled[0] == unlabelled[0] == "File L24-01403"

    def test_zoning_case_number_from_title(self):
        assert extract_identifier(
            "Zoning Case #26-042 re: 1408 Edgerly Ave.",
            "Amending the Zoning Ordinance and Map of the City of Albany.",
        ) == ("Case 26-042", "Case")

    def test_identifier_classes_are_namespaced(self):
        """A contract number and a case number can collide numerically in one city."""
        contract = extract_identifier("Contract No. 250811 - to provide services.")
        case = extract_identifier("Case No. 25-0811 heard by the Special Magistrate.")

        assert contract[0] != case[0]

    def test_dangling_suffix_is_not_captured(self):
        """A dash with nothing after it is punctuation, not an amendment suffix."""
        assert extract_identifier("Contract No. 210044- see attached.")[0] == "Contract 210044"
        assert extract_identifier("Contract No. 6006718-100% City Funding")[0] == "Contract 6006718"

    def test_master_contract_umbrella_is_skipped(self):
        """Alameda County cites both; the master is a vendor-level umbrella.

        Keying on it would merge every distinct agreement with that vendor into
        one matter under one canonical summary.
        """
        both = extract_identifier(
            "execute a contract (Master Contract No. 902683; Procurement Contract No. 30090) "
            "with a provider of psychological evaluation services"
        )
        umbrella_only = extract_identifier(
            "Approve a Master Contract with Family Bridges, Inc. (Master Contract No. 900174)"
        )

        assert both[0] == "Contract 30090"
        assert umbrella_only is None

    @pytest.mark.parametrize("separator", [" ", "  ", "\t", "\n"])
    def test_master_contract_guard_handles_source_whitespace(self, separator):
        assert extract_identifier(
            f"Approve a Master{separator}Contract No. 900174"
        ) is None

    def test_title_is_searched_before_body(self):
        assert extract_identifier(
            "Contract No. 6007968 award",
            "Contract No. 9999999 is referenced in passing.",
        )[0] == "Contract 6007968"

    @pytest.mark.parametrize(
        "text",
        [
            "Contract Period: Upon City Council Approval for a Period of Five (5) Years.",
            "Total Contract Amount: $12,523,254.11 for the term ending 2030.",
            "To amend Chapter 12 of the 2019 Detroit City Code, Article II, Section 4-1-1.",
            "Neighborhood Enterprise Zone Certificate for a house at 4128 Fourth Street.",
            "Pursuant to Public Act 146 of 2000 the department submits its report.",
            "",
        ],
    )
    def test_no_identifier_invented_from_unlabelled_numbers(self, text):
        """A wrong key permanently merges unrelated items under one canonical summary."""
        assert extract_identifier(text) is None


class TestAdapterTitleFileNumber:
    def test_title_file_number_is_still_vendor_extracted(self):
        """Raleigh's title prefix is the instance's own filing scheme."""
        html = container(
            title_row("1", "Recombination - BOA-0039-2025 (John Smith)")
            + '<div class="AgendaItemContentRow"><div class="AgendaItemDescription">'
            "Staff Contact: Collette Kinane, Senior Preservation Planner.</div></div>"
        )
        item = parse(html)[0]

        assert item["matter_file"] == "BOA-0039-2025"
        assert item["matter_type"] == "Board of Adjustment"


class TestMatterWorkGate:
    """A matter keyed off body text must not be tombstoned for having no documents."""

    class Appearance:
        def __init__(self, body_text=None, title="Item", attachments=None):
            self.body_text = body_text
            self.title = title
            self.attachments = attachments or []
            self.meeting_id = "m1"
            self.sequence = 1
            self.id = "i1"

    def test_body_only_matter_is_summarizable(self):
        body = (
            "Contract No. 6007968 - 100% City Funding to provide a digital evidence "
            "management solution for body-worn cameras."
        )
        work = MatterWorkSnapshot.from_appearances([self.Appearance(body_text=body)])

        assert work.is_summarizable
        assert work.best_body_text == body

    def test_stub_body_is_not_work(self):
        work = MatterWorkSnapshot.from_appearances([self.Appearance(body_text="Adjourn")])

        assert not work.is_summarizable

    def test_longest_body_wins_across_appearances(self):
        short = "Contract No. 6007968 for body-worn camera systems."
        long = short + " Contractor: Motorola Solutions Inc. Total: $12,523,254.11."
        work = MatterWorkSnapshot.from_appearances(
            [self.Appearance(body_text=short), self.Appearance(body_text=long)]
        )

        assert work.best_body_text == long

    def test_substantive_body_text_changes_work_version(self):
        """An amended inline record must not reuse the old canonical summary."""
        first = MatterWorkSnapshot.from_appearances(
            [
                self.Appearance(
                    body_text="Contract No. 6007968 authorizes body-worn camera systems."
                )
            ]
        )
        amended = MatterWorkSnapshot.from_appearances(
            [
                self.Appearance(
                    body_text=(
                        "Contract No. 6007968 authorizes body-worn camera systems "
                        "and increases the spending cap by $2 million."
                    )
                )
            ]
        )
        attachment_only = MatterWorkSnapshot.from_appearances([self.Appearance()])

        assert first.work_version != amended.work_version
        # No marker is added for the legacy attachment-only shape, avoiding a
        # database-wide reprocess wave for matters whose inputs did not expand.
        assert attachment_only.body_text_version is None

    def test_normalize_body_text_collapses_and_gates(self):
        assert normalize_body_text("  Contract   No.\n6007968 for body-worn camera systems. ") == (
            "Contract No. 6007968 for body-worn camera systems."
        )
        assert normalize_body_text("Recess") == ""
        assert normalize_body_text(None) == ""
