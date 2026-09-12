"""Vote evidence inside one agenda item's slice of the minutes.

Three shapes carry votes in the corpus, in descending attribution power:

  named lists   "Aye: 12 - Flynn, Gilmore, ..." / "Ayes: Supervisor Tam - Two (2)"
                "For: 4 - Champine, ..."  "Noes:"  "Absent: 1 - Romero Campbell"
  tallies       "Motion carried (6-0)." / "Vote: 3-0-0" / "Motion passed 2/0"
                "The motion carried, 6-0." / "Vote 6-0."
  results       "The motion carried by the following vote:" / "MOTION CARRIED"
                "carried unanimously" / "The motion failed"

A result anchors an Evidence. Named lists within the following lines attach
to it; a tally on the same or next line attaches to it. The publish gate in
engine.py decides what any of it is worth.

Confidence 7/10 on the regexes: every alternation came from a document in
the 2026-09-11 reservoir survey, and the gate refuses anything the arithmetic
cannot confirm.
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from parsing.rollcall.names import split_names, clean_name, looks_like_name

CATEGORY_CANON = {
    "for": "AYE", "aye": "AYE", "ayes": "AYE", "yes": "AYE", "yea": "AYE", "yeas": "AYE",
    "in favor": "AYE", "favor": "AYE", "approve": "AYE",
    "against": "NO", "nay": "NO", "nays": "NO", "no": "NO", "noes": "NO", "opposed": "NO",
    "abstain": "ABSTAIN", "abstained": "ABSTAIN", "abstaining": "ABSTAIN",
    "abstention": "ABSTAIN", "abstentions": "ABSTAIN",
    "not present": "ABSENT", "absent": "ABSENT", "excused": "EXCUSED", "recuse": "RECUSED", "recused": "RECUSED", "recusal": "RECUSED",
    "present": "PRESENT", "not voting": "NONVOTING",
    # Recorded dissent that the clerk formats as its own label. Missing these
    # publishes a unanimous vote over a dissent, the one error that matters most.
    "deemed nay": "NO", "deemed no": "NO", "deemed aye": "AYE", "deemed yes": "AYE",
    "voting no": "NO", "voting nay": "NO", "voting aye": "AYE", "voting yes": "AYE",
    "dissenting": "NO",
}
_CATEGORY_WORDS = "|".join(sorted((re.escape(k) for k in CATEGORY_CANON), key=len, reverse=True))
CATEGORY_LINE_RE = re.compile(
    rf"^[ \t]*(?P<cat>{_CATEGORY_WORDS})(?:\s*[:,–-]\s*|\s+(?=\d))(?:(?P<count>\d+)\s*(?:[-–]\s*|$))?(?P<rest>.*)$",
    re.IGNORECASE,
)
_TRAILING_COUNT_RE = re.compile(
    r"\s*[-–]?\s*(?:(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen)\s*)?\((?P<count>\d+)\)\s*\.?\s*$",
    re.IGNORECASE,
)
_ANY_LABEL_RE = re.compile(r"^[ \t]*[A-Za-z][A-Za-z /-]{0,35}:\s*")
_INLINE_LABEL_SPLIT_RE = re.compile(rf",\s*(?=(?:{_CATEGORY_WORDS})\s*[:–-])", re.I)
_INLINE_COUNT_RE = re.compile(rf"(?P<cat>{_CATEGORY_WORDS})\s+(?P<count>\d+)\b", re.I)
_BARE_COUNT_RE = re.compile(r"^\s*(\d+)\s*[-–]?\s*$")
_NONE_RE = re.compile(r"^\s*\(?\s*(?:none|nil|n/a|-|0)\s*\)?\s*\.?\s*$", re.IGNORECASE)
# "Abstain-None. Moved by Ald. Ben Delie, seconded by ..." -- the sentence after
# the category ran into it and Delie was recorded as abstaining on a motion he had
# voted aye on, so the duplicate-person guard then withheld his ballot. The
# category is empty only when the full stop closes it; Petaluma extracts as
# "No: None Vice Mayor DeCarli, Councilmember Shribbs", where a neighbouring
# category's None bled in and two real no-votes follow it.
_NONE_THEN_SENTENCE_RE = re.compile(r"^\s*\(?\s*(?:none|nil|n/a)\s*\)?\s*\.", re.IGNORECASE)
_STRAY_NONE_RE = re.compile(r"^\s*\(?\s*(?:none|nil|n/a)\s*\)?[\s,;-]+(?=\S)", re.IGNORECASE)

RESULT_RE = re.compile(
    r"(?P<result>"
    r"(?:the\s+)?motion\s+(?:to\s+\w+\s+)?(?:carried|passed|prevailed|failed|was\s+(?:approved|adopted|defeated|denied)|(?:was\s+)?approved|(?:was\s+)?adopted|(?:was\s+)?denied)"
    r"|(?:carried|passed|failed|prevailed)\s+by\s+the\s*following\s*vote"
    r"|(?:this|the)\s+(?:item|matter|resolution|ordinance|motion)\s+was\s+(?:adopted|approved|passed|placed\s+on\s+file|referred|held|denied)"
    r"|vote[sd]?\s*[:\-–]?\s*\d{1,2}\s*[-–/]\s*\d{1,2}"
    # "Motion/second to approve by Commissioners Fuller/McCord carried 6-0":
    # the result word is nowhere near the word motion, the tally is the anchor.
    r"|(?:carried|passed|prevailed|failed|approved|adopted|denied)\s+\(?\d{1,2}\s*[-–/]\s*\d{1,2}"
    # A bare disposition on its own line, closing an Ayes/Nays/Abstain block
    # (Clinton Township). Anchored to the line start so the word cannot be
    # picked out of running prose.
    r"|^[ \t]*(?:passed|failed|adopted|approved|denied|carried)[ \t]*\.?[ \t]*$"
    r"|result\s*:\s*(?:passed|failed|adopted|approved|denied|carried)"
    # "unanimous" only counts beside a vote word; "the unanimous request of
    # the Board" is prose, not a roll call.
    r"|(?:carried|passed|approved|adopted|voted|voting\s*:?|vote\s*:?)\s+unanimous(?:ly)?"
    r"|unanimous(?:ly)?\s+(?:carried|passed|approved|adopted|vote)"
    r")",
    re.IGNORECASE | re.MULTILINE,
)
_FAIL_RE = re.compile(r"\b(?:failed|defeated|denied|did\s+not\s+(?:carry|pass))\b", re.IGNORECASE)
_PASS_RE = re.compile(r"\b(?:carried|passed|prevailed|approved|adopted)\b", re.IGNORECASE)
TALLY_RE = re.compile(
    # The trailing guard rejects a longer number or a decimal continuation
    # ("6-0.5"), but a sentence-ending period is not one ("carried 5-0.").
    r"(?<![\d./-])(?P<yes>\d{1,2})\s*(?:[-–/]|\bto\b)\s*(?P<no>\d{1,2})"
    r"(?:\s*[-–/]\s*(?P<third>\d{1,2}))?(?!\d|[./-]\d)",
    re.IGNORECASE,
)
# Who moved and seconded. Used as a cheap membership check: a motion moved by
# someone who is not on the roster in play means this passage belongs to a
# different body than the one we are about to attribute it to.
# The lead-in is case-insensitive; the name is not, so a sentence fragment
# cannot masquerade as a mover.
# A surname is not ASCII. "Lomelí" captured as "Lomel" and "Peña" as "Pe", so the
# mover resolved to nobody and the membership gate rejected the whole motion --
# 65 of Cudahy's ballots. The first character is a letter that is not lowercase,
# which covers an accented capital without naming ranges.
_NAME_WORD = r"(?![a-z])[^\W\d_](?:[^\W\d_]|['\u2019-])*"
_MOVER_RE = re.compile(
    r"(?i:motion\s+(?:was\s+)?(?:made\s+)?by|(?:it\s+was\s+)?moved\s+by|on\s+a\s+motion\s+(?:of|by)"
    r"|motion\s+offered\s+by|motion\s+of|duly\s+seconded\s+by|seconded\s+by|second\s+by|supported\s+by)"
    # A lowercase office word may sit between the title and the surname
    # ("Council member Lewis", "Board Member Smith").
    rf"\s+(?P<name>{_NAME_WORD}\.?(?:\s+(?:member|members|president|chair|pro\s+tem))?"
    rf"(?:\s+{_NAME_WORD}\.?){{0,3}})",
)
# "X moved, seconded by Y" / "Councilmember Morris seconded the motion"
_MOVED_SUFFIX_RE = re.compile(
    rf"\b(?P<name>{_NAME_WORD}(?:\s+{_NAME_WORD}){{0,2}})\s+(?:moved|seconded)\b"
)
# The minutes credit a body with acting in several shapes, and the old detector
# wanted one exact lead-in: it found 16 of the 1,326 mentions in the corpus.
# Each word of the name must be capitalized, so the match cannot run back across
# "At its June 24th meeting, the ..." into the sentence before it. "The Committee"
# and "The Board" alone are anaphora for the body already sitting, not a second
# actor, so a bare article form is not a name.
_BODY_NAME = (r"(?:[A-Z][\w'\-]*|of|and|for|the|&)"
              r"(?:\s+(?:[A-Z][\w'\-]*|of|and|for|the|&)){0,7}"
              r"\s+(?:Committee|Commission|Board|Authority)")
_REPORTED_BODY_RE = re.compile(
    # "recommended by the X Committee", "referred to the X Commission"
    r"(?:\brecommended\s+by|\breferred\s+to|\b(?:on|per)\s+the\s+recommendation\s+of"
    r"|\b(?:adopted|approved|passed|denied)\s+by)"
    rf"\s+(?:the\s+)?(?P<body>{_BODY_NAME})\b"
    # "the X Committee voted 5-2 to recommend denial", "X Board introduced"
    rf"|\b(?P<body2>{_BODY_NAME})\s+(?:unanimously\s+)?(?:voted[^.]{{0,40}}?\s+to\s+)?"
    r"(?:recommend\w*|moved|introduced|sponsored|submitted)\b",
)
_ANAPHORIC_BODY_RE = re.compile(
    r"^(?:the\s+)?(?:committee|commission|board|council|authority)$", re.IGNORECASE
)
_PROCEDURAL_MOTION_RE = re.compile(
    r"\bto\s+(?:adjourn|recess|reconvene|return\s+to\s+open\s+session|(?:go|convene|enter)\s+into\s+(?:closed|executive)\s+session)\b"
    r"|\bconvene\s+into\s+(?:closed|executive)\s+session\b"
    r"|\bthe\s+(?:meeting|board|committee|council)\s+be\s+adjourned\b|\badjournment\b",
    re.IGNORECASE,
)

_ABSTENTION_MENTION_RE = re.compile(r"\babstain(?:ed|s|ing)?\b|\babstentions?\b|\brecus(?:ed|al)\b", re.IGNORECASE)
# Unanimity beside a vote word, wherever it sits in the sentence: the result
# anchor itself often stops at "carried", before "unanimously".
_UNANIMOUS_RE = re.compile(
    r"(?:carried|passed|approved|adopted|voted|voting|vote|consent)\s*:?\s*(?:\w+\s+){0,2}unanimous(?:ly)?"
    r"|unanimous(?:ly)?\s+(?:carried|passed|approved|adopted|consent)",
    re.IGNORECASE,
)
# "approved 8/11/26" puts a context word right before a date, so the context
# guard alone cannot reject it. Every three-part all-slash number in the saved
# corpus is a date (18,111 of them end in 26) and none is a vote, so the shape
# itself is the test.
_DATE_SHAPED_RE = re.compile(r"^\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{1,2}$")
# Bainbridge Island prints the winning side first: "the motion failed 6-1 with
# Councilmember Nelson voting in favor" is one aye and six noes, not six ayes.
# The printed pair alone cannot reveal that -- a real supermajority threshold
# looks identical -- so the order is only corrected when the sentence names the
# supporters and that count matches the second number instead of the first.
_IN_FAVOR_RE = re.compile(
    r"\bwith\s+(?P<names>[^.]{3,200}?)\s+voting\s+in\s+favor", re.IGNORECASE
)
_TALLY_CONTEXT_RE = re.compile(
    r"(?:vote[sd]?\s+(?:of\s+)?|vote[sd]?|voting|carried|passed|prevailed|failed|motion|approved|adopted|denied|unanimously|result|\(|\[)\s*[:,]?\s*$",
    re.IGNORECASE,
)


@dataclass
class Section:
    value: str
    names: List[str]
    stated: Optional[int]
    raw_text: str = ""
    raw_label: str = ""


@dataclass
class Evidence:
    result_text: str
    outcome: Optional[str]                 # PASS / FAIL / None
    offset: int                            # start of result_text in the block
    sections: List[Section] = field(default_factory=list)
    tally: Optional[Tuple[int, int, int]] = None  # (yes, no, other)
    unanimous: bool = False
    movers: List[str] = field(default_factory=list)
    source_start: int = 0
    source_end: int = 0
    qualifications: List[str] = field(default_factory=list)
    disposition: Optional[str] = None  # Subject disposition is not a motion outcome.
    reported_body: Optional[str] = None    # A body the minutes credit with acting.
    procedural: bool = False               # a motion to adjourn, recess, reconvene

    @property
    def named(self) -> bool:
        return any(s.names for s in self.sections)


def _parse_sections(lines: List[str], start: int, limit: int) -> List[Section]:
    sections: List[Section] = []
    i = start
    while i < min(len(lines), start + limit):
        inline = list(_INLINE_COUNT_RE.finditer(lines[i]))
        if len(inline) > 1 and not _INLINE_COUNT_RE.sub("", lines[i]).strip(" ,.;\t"):
            if not sections:
                sections.extend(Section(CATEGORY_CANON[m['cat'].lower()], [], int(m['count']),
                                        lines[i], m['cat']) for m in inline)
            i += 1
            continue
        m = CATEGORY_LINE_RE.match(lines[i])
        if not m:
            if RESULT_RE.search(lines[i]):
                break
            if sections and lines[i].strip() == "":
                # one blank line inside a list is layout; two end it
                if i + 1 < len(lines) and lines[i + 1].strip() == "":
                    break
            i += 1
            continue
        value = CATEGORY_CANON[m.group("cat").lower()]
        stated = int(m.group("count")) if m.group("count") else None
        blob_parts = [m.group("rest").strip()]
        j = i + 1
        while j < len(lines) and j < i + 30:
            nxt = lines[j]
            if _ANY_LABEL_RE.match(nxt) or CATEGORY_LINE_RE.match(nxt) or RESULT_RE.search(nxt) or nxt.strip() == "":
                break
            bare = _BARE_COUNT_RE.match(nxt)
            if bare:
                stated = int(bare.group(1))
                j += 1
                break
            blob_parts.append(nxt.strip())
            j += 1
        blob = " ".join(p for p in blob_parts if p)
        inline_parts = _INLINE_LABEL_SPLIT_RE.split(blob)
        blob = inline_parts[0]
        tc = _TRAILING_COUNT_RE.search(blob)
        if tc:
            stated = int(tc.group("count")) if stated is None else stated
            blob = blob[:tc.start()]
        vertical = [clean_name(part) for part in blob_parts if part]
        if (len(inline_parts) == 1 and len(vertical) > 1 and not re.search(r"[,;]", blob)
                and all(looks_like_name(name) for name in vertical)
                and any(clean_name(part) != part for part in blob_parts if part)):
            names = vertical
        elif _NONE_RE.match((blob_parts[0] or "").strip()):
            # The category's own line says none. Bend prints "No: none" and then
            # "Councilor Norris was recused." on the next line: that sentence is
            # not a no-vote, and reading it as one made the section contradict a
            # printed 6-0 and withheld the whole roll call.
            names = []
        elif _NONE_THEN_SENTENCE_RE.match(blob or ""):
            names = []
        else:
            # A stray None in front of real names is the neighbouring category
            # bleeding in, not this one's value.
            names = split_names(_STRAY_NONE_RE.sub("", blob or ""))
        if stated is None and not names:
            if (not blob or _NONE_THEN_SENTENCE_RE.match(blob) or _NONE_RE.match(blob)
                    or _NONE_RE.match((blob_parts[0] or "").strip())):
                stated = 0
            else:
                # Unreadable category content is not a recorded zero.
                i = j
                continue
        sections.append(Section(value=value, names=names, stated=stated,
                                raw_text="\n".join(lines[i:j]), raw_label=m.group("cat")))
        if len(inline_parts) > 1:
            sections.extend(_parse_sections(inline_parts[1:], 0, len(inline_parts) - 1))
        i = j
    return sections


def _reported_body(context: str) -> Optional[str]:
    """The body the minutes credit with acting, or None when it is this one."""
    match = _REPORTED_BODY_RE.search(context)
    if not match:
        return None
    name = (match.group("body") or match.group("body2") or "").strip(" ,.;-")
    name = re.sub(r"^(?:the|a)\s+", "", re.sub(r"\s+", " ", name), flags=re.IGNORECASE)
    if _ANAPHORIC_BODY_RE.match(name):
        return None
    return name or None


def _orient_tally(tally, outcome, context):
    """Swap a failed motion's pair when the named supporters match the second number."""
    if not tally or outcome != "FAIL" or tally[0] <= tally[1]:
        return tally
    m = _IN_FAVOR_RE.search(context)
    if not m:
        return tally
    supporters = len(split_names(m.group("names")))
    if supporters == tally[1] and supporters != tally[0]:
        return (tally[1], tally[0], tally[2])
    return tally


def _tally_near(lines: List[str], idx: int, result_text: str) -> Optional[Tuple[int, int, int]]:
    candidates = [result_text] + lines[idx:idx + 2]
    for text in candidates:
        for m in TALLY_RE.finditer(text):
            before = text[:m.start()]
            if _TALLY_CONTEXT_RE.search(before) and not _DATE_SHAPED_RE.match(m.group(0).strip()):
                yes, no = int(m.group("yes")), int(m.group("no"))
                third = int(m.group("third")) if m.group("third") else 0
                if yes + no + third <= 60:
                    return yes, no, third
    return None


def find_evidence(block: str) -> List[Evidence]:
    """Every result anchor in the block with its attached lists and tally."""
    raw_lines = block.splitlines(keepends=True)
    lines = [line.splitlines()[0] for line in raw_lines]
    offsets = []
    pos = 0
    for line in raw_lines:
        offsets.append(pos)
        pos += len(line)
    found: List[Evidence] = []
    for idx, line in enumerate(lines):
        for m in RESULT_RE.finditer(line):
            result_text = m.group("result")
            window = " ".join(lines[idx:idx + 2])
            outcome = None
            disposition = "denied" if re.search(r"\bdenied\b", result_text, re.I) else None
            if disposition:
                # A denied application/appeal may be the result of a successful
                # motion to deny. Only an explicitly denied *motion* fails.
                if re.search(r"\bmotion\s+(?:was\s+)?denied\b", result_text, re.I):
                    outcome = "FAIL"
            elif _FAIL_RE.search(result_text):
                outcome = "FAIL"
            elif _PASS_RE.search(result_text):
                outcome = "PASS"
            ev = Evidence(
                result_text=re.sub(r"\s+", " ", line.strip())[:300],
                outcome=outcome,
                disposition=disposition,
                offset=offsets[idx] + m.start(),
                source_start=offsets[max(0, idx - 8)],
                source_end=offsets[min(len(lines)-1, idx + 20)] + len(lines[min(len(lines)-1, idx + 20)]),
                unanimous=bool(_UNANIMOUS_RE.search(window)),
            )
            # Only the sentence carrying this result may credit a body. A block
            # mention cannot be applied to every motion in the block: an item
            # whose narrative says the Planning Commission recommended denial
            # usually continues with the council's own vote, and crediting that
            # vote to the commission is the mis-attribution the withhold existed
            # to prevent. A bare mention is a fact about the matter's referral,
            # not about any motion here.
            ev.reported_body = _reported_body(
                line + " " + " ".join(lines[idx + 1:idx + 3]))
            if ev.reported_body:
                ev.qualifications.append('reported_committee_action')
            ev.sections = _parse_sections(lines, idx + 1, 14)
            if not ev.sections and idx > 0:
                # Alameda County prints the lists before "Motion passed 2/0"
                prior = _parse_sections(lines, max(0, idx - 8), 8)
                if prior and all(s.value != "PRESENT" for s in prior):
                    ev.sections = prior
            # Look back a few lines: the motion sentence usually precedes
            # the result, and both sit inside the same passage.
            context = " ".join(lines[max(0, idx - 4):idx + 2])
            ev.movers = [m.group("name") for m in _MOVER_RE.finditer(context)]
            ev.movers += [m.group("name") for m in _MOVED_SUFFIX_RE.finditer(context)]
            ev.procedural = bool(_PROCEDURAL_MOTION_RE.search(context))
            ev.tally = _orient_tally(_tally_near(lines, idx, line), outcome,
                                     " ".join(lines[idx:idx + 3]))
            # "Motion Passed 5-0 with one abstention": somebody present did
            # not vote aye, so attendance arithmetic cannot name the ayes.
            if _ABSTENTION_MENTION_RE.search(window):
                ev.unanimous = False
            if _ABSTENTION_MENTION_RE.search(window):
                ev.qualifications.append(window)
            found.append(ev)
            break
    return found


def _dedupe(evidence: List[Evidence]) -> List[Evidence]:
    """A motion sentence and its 'by the following vote' line are one event."""
    out: List[Evidence] = []
    for ev in evidence:
        if out and ev.offset - out[-1].offset < 160 and not out[-1].sections and not out[-1].tally:
            out[-1] = ev if (ev.sections or ev.tally or ev.outcome) else out[-1]
            continue
        out.append(ev)
    return out
