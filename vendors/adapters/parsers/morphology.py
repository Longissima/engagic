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

from typing import Any, Mapping, Optional, Tuple, Union

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
BARE_MAX_PAGES = 1            # one page: there is no packet behind it
BARE_MAX_EXTERNAL = 0         # one link means one staff report worth reading
BARE_MAX_TEXT_CHARS = 2000    # above this the page carries prose, not a list
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


def is_bare_document(
    profile: Union[PdfProfile, Mapping[str, Any], None],
) -> bool:
    """One short page that links nowhere -- a listing of titles, not a record.

    Orthogonal to classify(): that names what structure a document HAS, this
    answers whether anything is behind it. A retreat agenda listing seven
    discussion topics is a legitimate FLAT_TEXT_AGENDA whose items are real
    and whose content does not exist. Callers use it to decline summarizing
    rather than let a model write paragraphs from a title.

    The text ceiling is what separates a list from a page of prose: 2,086 of
    the 2,252 one-page link-free meetings in prod carry under 2k chars, where
    a summary would run as long as the document it summarizes. The 27 that
    pack >4k onto one page are real single-page agendas with per-item
    recommendations, and they stay summarizable. Confidence 7/10 on the
    threshold itself -- retune against the audit, not against one city.

    Accepts the live dataclass (during chunking) or its persisted dict form
    (replayed later from the chunk audit).
    """
    if profile is None:
        return False
    if isinstance(profile, PdfProfile):
        page_count = profile.page_count
        external_links = profile.external_links
        text_chars = profile.text_chars
    else:
        page_count = profile.get("page_count") or 0
        external_links = profile.get("external_links") or 0
        text_chars = profile.get("text_chars") or 0
    return (
        page_count <= BARE_MAX_PAGES
        and external_links <= BARE_MAX_EXTERNAL
        and text_chars <= BARE_MAX_TEXT_CHARS
    )
