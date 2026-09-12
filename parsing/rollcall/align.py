"""Align a minutes document to the meeting's own agenda items.

Minutes are generated from the agenda we already parsed, so the candidate
set for any vote is the handful of items in that one meeting and the
question is only where each item starts in the text. Ladder, most to least
reliable: the matter file number printed beside the item, the agenda number
at a line start, the item title itself. Anchors must appear in agenda order;
an out-of-order match is a false positive and is dropped (longest increasing
subsequence over document offsets).

Confidence 7/10. Title matching is the weak rung: chunker titles can be
garbage for PDF-born agendas, and then the item simply has no block.
"""

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence

from parsing.rollcall.evidence import CATEGORY_LINE_RE, RESULT_RE

_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "to", "for", "in", "on", "at", "by", "with",
    "from", "as", "is", "be", "that", "this", "city", "county", "council", "approve",
    "approval", "consider", "consideration", "action", "item", "regarding", "re",
}


@dataclass
class Anchor:
    item: Any
    start: int
    rung: str


def _file_pattern(matter_file: str) -> Optional[re.Pattern]:
    core = matter_file.split(" ", 1)[1] if " " in matter_file else matter_file
    core = core.strip()
    if len(core) < 4:
        return None
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(core) + r"(?![A-Za-z0-9])", re.IGNORECASE)


def _agenda_pattern(agenda_number: str) -> Optional[re.Pattern]:
    number = agenda_number.strip().rstrip(".)")
    if not number or len(number) > 12:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)+", number) and not re.fullmatch(r"\d{1,2}\.[A-Za-z0-9]{1,2}", number):
        return None  # "43.7" / "732.50" are measurements the chunker mistook for numbering
    if re.fullmatch(r"\d{1,2}|[a-zA-Z]", number):
        # "7." or "a." at a line start is unique enough only with a word after it
        return re.compile(r"^[ \t]*" + re.escape(number) + r"[.)]\s+(?=[A-Za-z])", re.MULTILINE)
    return re.compile(r"^[ \t]*" + re.escape(number) + r"[.)]?\s+(?=[A-Za-z(\"'])", re.MULTILINE)


def _title_words(title: str) -> List[str]:
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", title.lower()) if w not in _STOPWORDS]
    return words[:8]


def _title_match(text_lower: str, lines_lower: List[str], line_offsets: List[int], title: str) -> Optional[int]:
    words = _title_words(title)
    if len(words) < 3:
        return None
    probe = r"\W+(?:\w+\W+){0,3}?".join(re.escape(w) for w in words[:5])
    m = re.search(probe, text_lower, re.IGNORECASE)
    if m:
        return m.start()
    # Fuzzy fallback only over lines that share the title's rarest word;
    # SequenceMatcher over every line of a 40-page document is quadratic.
    target = " ".join(words)
    rare = max(words, key=len)
    best, best_ratio = None, 0.0
    for offset, line in zip(line_offsets, lines_lower):
        if len(line) < 20 or rare not in line:
            continue
        matcher = SequenceMatcher(None, target, " ".join(_title_words(line)))
        if matcher.quick_ratio() < 0.8:
            continue
        ratio = matcher.ratio()
        if ratio > best_ratio:
            best, best_ratio = offset, ratio
    return best if best_ratio >= 0.8 else None


def _longest_increasing(anchors: List[Anchor]) -> List[Anchor]:
    if not anchors:
        return anchors
    n = len(anchors)
    best = [1] * n
    prev = [-1] * n
    for i in range(n):
        for j in range(i):
            if anchors[j].start < anchors[i].start and best[j] + 1 > best[i]:
                best[i], prev[i] = best[j] + 1, j
    end = max(range(n), key=lambda k: best[k])
    chain = []
    while end != -1:
        chain.append(anchors[end])
        end = prev[end]
    return list(reversed(chain))


def anchor_items(text: str, items: Sequence[Any]) -> List[Anchor]:
    """Items in agenda order with their start offset in the minutes, where found.

    Each item is a mapping with title, agenda_number, matter_file, sequence.
    """
    text_lower = text  # Preserve original Unicode offsets; regex handles case.
    raw_lines = text.splitlines(keepends=True)
    lines = [line.splitlines()[0] for line in raw_lines]
    line_offsets, pos = [], 0
    for line in raw_lines:
        line_offsets.append(pos)
        pos += len(line)
    lines_lower = [line.lower() for line in lines]

    anchors: List[Anchor] = []
    for item in sorted(items, key=lambda it: it.get("sequence") or 0):
        start, rung = None, ""
        if item.get("matter_file"):
            pattern = _file_pattern(str(item["matter_file"]))
            m = pattern.search(text) if pattern else None
            if m:
                start, rung = m.start(), "matter_file"
        if start is None and item.get("title"):
            # Compact case numbers are often present only in the title.
            # Match the complete token, never its alphabetic prefix (DRB260012).
            code = re.match(r"^\s*([A-Za-z]{1,8}\d{4,10})\b", str(item["title"]))
            pattern = _file_pattern(code.group(1)) if code else None
            match = pattern.search(text) if pattern else None
            if match:
                start, rung = match.start(), "title_identifier"
        if start is None and item.get("agenda_number"):
            pattern = _agenda_pattern(str(item["agenda_number"]))
            m = pattern.search(text) if pattern else None
            if m:
                start, rung = m.start(), "agenda_number"
        if start is None and item.get("title"):
            found = _title_match(text_lower, lines_lower, line_offsets, str(item["title"]))
            if found is not None:
                start, rung = found, "title"
        if start is not None:
            # Snap to the line start so the previous block never swallows
            # this item's numbering ("b.") as a trailing name fragment.
            start = text.rfind("\n", 0, start) + 1
            anchors.append(Anchor(item=item, start=start, rung=rung))
    return _longest_increasing(anchors)


# Procedural headings that carry their own motions (adjourn, reconvene,
# approve the agenda) but rarely survive as agenda items. Any of them closes
# the block that precedes it, so its motion is credited to nothing rather
# than to the last substantive item.
_BREAK_RE = re.compile(
    r"^[ \t]*(?:\d+\.?|[A-Z]\.|[IVX]+\.|[a-z]\.)?\s*(?:"
    r"ADJOURN(?:MENT)?\b|Meeting adjourned|The meeting (?:was )?adjourned|NEXT MEETING\b|"
    r"RECONVENE\b|CLOSED SESSION\b|EXECUTIVE SESSION\b|RECESS\b|"
    r"APPROVAL OF (?:THE )?(?:AGENDA|MINUTES)\b|CALL TO ORDER\b|ROLL CALL\b|PLEDGE\b|"
    r"PUBLIC COMMENTS?\b|CITIZEN COMMENTS?\b|ANNOUNCEMENTS\b)",
    re.MULTILINE | re.IGNORECASE,
)


# A short line with no lowercase letters is a section heading ("VII.
# ORDINANCES", "B. SECOND READING"). Crossing one means the block has left
# its item, so it ends there; losing a vote beats crediting it to the wrong
# item. A document typeset entirely in capitals yields no blocks at all,
# which abstains rather than guesses.
_CAPS_HEADING_RE = re.compile(r"^[ \t]*(?=[^a-z\n]*[A-Z])[A-Z0-9 .()\-&/',:]{4,60}$", re.MULTILINE)


# Our own extractor stamps a page separator into the corpus text it writes
# (parsing/pdf.py). It is short, all-caps and built from the same characters
# as a section heading, so the heading rule below read every page break as a
# new section: an item heading on one page lost the motion printed on the
# next. Measured on 142 minutes documents, this accounted for 306 of 638
# early block ends and discarded real vote evidence 74 times.
#
# The marker is skipped in place rather than stripped from the text, because
# published votes carry byte-offset receipts into exactly this string and
# rewriting it would invalidate every one of them.
_PAGE_MARKER_RE = re.compile(r"^[ \t]*-{2,}\s*PAGE\s+\d+\s*-{2,}[ \t]*$")

# A caps line that announces the vote itself is scaffolding around the motion,
# not a new section. "D. MOTION" and "VOTE:" were each cutting an item away
# from the vote printed directly beneath them.
_VOTE_SCAFFOLD_RE = re.compile(
    r"^[ \t]*(?:[A-Z0-9]{1,4}[.)]\s*)?(?:MOTION|VOTE|ROLL\s*CALL\s*VOTE|RECORDED\s+VOTE)\b[ \t]*:?[ \t]*$",
    re.IGNORECASE,
)


def _repeated_lines(text: str, threshold: int = 3) -> frozenset:
    """Lines a document repeats are running headers, not section headings.

    Letterhead reprinted on every page ("CITY OF MARLBOROUGH") is typeset in
    capitals and otherwise identical to a heading. A real section heading
    appears once. Counting is done per document and cached by the caller.
    """
    counts: Dict[str, int] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if len(stripped) >= 4:
            counts[stripped] = counts.get(stripped, 0) + 1
    return frozenset(line for line, n in counts.items() if n >= threshold)


def _caps_heading(
    text: str, start: int, end: int, furniture: frozenset = frozenset()
) -> Optional[int]:
    """First all-caps heading in the range that is not itself vote evidence.

    Clerks print the record in capitals too ("RESULT: APPROVED BY UNANIMOUS
    CONSENT", "NAYS: NONE"); cutting there would sever every block from the
    vote it exists to carry. Our own page separator, the labels that announce
    a vote, and any line the document repeats are likewise not headings.
    """
    for match in _CAPS_HEADING_RE.finditer(text, start, end):
        line = match.group(0)
        if CATEGORY_LINE_RE.match(line) or RESULT_RE.search(line):
            continue
        if _PAGE_MARKER_RE.match(line) or _VOTE_SCAFFOLD_RE.match(line):
            continue
        if line.strip() in furniture:
            continue
        return match.start()
    return None


def _first_break(
    text: str, start: int, end: int, furniture: frozenset = frozenset()
) -> Optional[int]:
    cuts = [m.start() for m in (_BREAK_RE.search(text, start, end),) if m]
    caps = _caps_heading(text, start, end, furniture)
    if caps is not None:
        cuts.append(caps)
    return min(cuts) if cuts else None


def blocks(text: str, anchors: List[Anchor]) -> List[Dict[str, Any]]:
    """[{item, start, end, rung}] covering each anchored item to the next
    anchor or the next section heading, whichever comes first."""
    out = []
    furniture = _repeated_lines(text)
    for i, anchor in enumerate(anchors):
        end = anchors[i + 1].start if i + 1 < len(anchors) else len(text)
        # Skip past the anchor's own line so its heading never ends its block.
        body = text.find("\n", anchor.start)
        if body == -1 or body >= end:
            out.append({"item": anchor.item, "start": anchor.start, "end": end, "rung": anchor.rung})
            continue
        cut = _first_break(text, body + 1, end, furniture)
        if cut is not None:
            end = cut
        out.append({"item": anchor.item, "start": anchor.start, "end": end, "rung": anchor.rung})
    return out
