"""Morphology classifier: profile -> named document shape -> suggested rung.

THE single home for detection thresholds. The engines' scattered inline
heuristics (~60 comparisons across v1/v2, three competing definitions of
"has a TOC") get replaced by this table as confidence grows; until then
the classifier runs in shadow — every classification and whether it
agreed with the cascade's eventual winner lands in the chunk audit, so
prod accumulates a confusion matrix while risking nothing.

Its one active power: the suggested rung fills the cascade's hint slot
when a city has no sticky routing history (new cities, cold starts).
Hints only reorder rungs within a ladder — never skip the cascade — so a
wrong suggestion costs exactly one wasted attempt, same as today.

Thresholds are corpus-derived (52 prod fixtures, 2026-06-11 census):
thin TOCs (1-2 entries) failed 8/9 — hence TOC_MIN_REAL_ENTRIES=3;
every current v2:url winner has >=3 external links — hence the
FLAT_TEXT external-link ceiling. Retune against prod audit data, not
against whichever city broke last week.
"""

from typing import Optional, Tuple

from vendors.adapters.parsers.pdf_profile import PdfProfile

# Named document shapes (the user's "10 different pdf versions", as data)
LINKED_AGENDA = "linked_agenda"        # hyperlinks to attachments are the structure
ANCHORED_PACKET = "anchored_packet"    # front pages jump to items inside the packet
TOC_PACKET = "toc_packet"              # big compiled packet, outline is the structure
TOC_AGENDA = "toc_agenda"              # short doc with item-level outline (nampa shape)
FLAT_TEXT_AGENDA = "flat_text_agenda"  # numbered heading lines are the only structure
SCANNED = "scanned"                    # image-only, nothing to anchor on
MONOLITH = "monolith"                  # text but no item structure (minutes, notices)

# --- the threshold table -----------------------------------------------------
TOC_MIN_REAL_ENTRIES = 3      # 1-2 entry outlines are navigation, not structure
TOC_PACKET_MIN_PAGES = 11     # the long-standing ">10 pages = packet" boundary
LINKED_MIN_EXTERNAL = 3       # fewer is footer/nav chrome, not agenda structure
ANCHORED_MIN_INTERNAL = 3     # mirrors v2's pageref gate
FLAT_TEXT_MIN_HEADINGS = 3    # mirrors text_chunker.MIN_ITEMS
FLAT_TEXT_MAX_EXTERNAL = 2    # >=3 links means LINKED_AGENDA owns the doc
FLAT_TEXT_MAX_PAGES = 20      # mirrors text_chunker.TEXT_AGENDA_MAX_PAGES
# ------------------------------------------------------------------------------

# Shape -> the rung most likely to win first try. None = no opinion, let
# the ladder run in its declared order.
_SUGGESTED_RUNG = {
    LINKED_AGENDA: "v2:url",
    ANCHORED_PACKET: "v2:auto",   # pageref lives inside v2's auto-detect
    TOC_PACKET: "v2:toc",
    TOC_AGENDA: "v2:toc",
    FLAT_TEXT_AGENDA: "text:auto",
    SCANNED: None,
    MONOLITH: None,
}


def classify(profile: PdfProfile) -> Tuple[str, Optional[str]]:
    """Map a measured profile to (morphology, suggested_rung).

    Rule order matters: structure signals beat scannedness (an outline
    slices page ranges without a text layer — nampa taught us single-page
    docs can carry a 13-entry item outline), and links beat headings
    (linked agendas usually also have numbered lines).
    """
    if profile.external_links >= LINKED_MIN_EXTERNAL:
        morphology = LINKED_AGENDA
    elif (
        profile.internal_links >= ANCHORED_MIN_INTERNAL
        and profile.page_count >= TOC_PACKET_MIN_PAGES
    ):
        morphology = ANCHORED_PACKET
    elif profile.toc_real_entries >= TOC_MIN_REAL_ENTRIES:
        morphology = (
            TOC_PACKET
            if profile.page_count >= TOC_PACKET_MIN_PAGES
            else TOC_AGENDA
        )
    elif not profile.has_text_layer:
        morphology = SCANNED
    elif (
        profile.item_number_lines >= FLAT_TEXT_MIN_HEADINGS
        and profile.page_count <= FLAT_TEXT_MAX_PAGES
        and profile.external_links <= FLAT_TEXT_MAX_EXTERNAL
    ):
        morphology = FLAT_TEXT_AGENDA
    else:
        morphology = MONOLITH

    return morphology, _SUGGESTED_RUNG[morphology]
