"""Minutes interpretation types, exact name resolution, and body alignment guards.

The observation pipeline discovers and retains evidence independently of item
alignment, validates each claim, and exposes a separate public projection.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from parsing.rollcall.attendance import Attendance
from parsing.rollcall.names import clean_name, fold


class Gazetteer:
    """Raw minutes name -> canonical roster name; ambiguous keys resolve to nothing."""

    def __init__(self, roster: Sequence[str]):
        keys: Dict[str, set] = {}
        # One canonical spelling per folded name, so accented and plain
        # variants of the same member do not make their surname ambiguous.
        by_fold: Dict[str, str] = {}
        surnames_only: List[str] = []
        for r in roster:
            cleaned = clean_name(r)
            if not cleaned:
                continue
            parts = fold(cleaned).split()
            if len(parts) == 1:
                surnames_only.append(cleaned)
                continue
            # "Klarissa J. Peña" and "Klarissa Peña" are one person: key on
            # first and last token so a middle initial cannot split them.
            by_fold.setdefault(parts[0] + " " + parts[-1], cleaned)
        # A bare surname from an attendance line ("Balducci") is the same
        # person as the roster's "Claudia Balducci"; only add it as its own
        # member when no full name carries that surname.
        known_last = {fold(full).split()[-1] for full in by_fold.values()}
        for surname in surnames_only:
            if fold(surname) not in known_last:
                by_fold.setdefault(fold(surname), surname)
        self.canonical = sorted(by_fold.values())
        for full in self.canonical:
            parts = fold(full).split()
            variants = {fold(full), parts[-1]}
            if len(parts) >= 2:
                variants.add(" ".join(parts[-2:]))
                variants.add(parts[0][0] + ". " + parts[-1])
            for v in variants:
                keys.setdefault(v, set()).add(full)
        self.keys = {k: next(iter(v)) for k, v in keys.items() if len(v) == 1}

    def resolve(self, raw: str) -> Optional[str]:
        k = fold(clean_name(raw))
        if not k:
            return None
        if k in self.keys:
            return self.keys[k]
        parts = k.split()
        if len(parts) >= 2:
            # A conflicting first name must not resolve through its surname.
            first_last = parts[0] + " " + parts[-1]
            matches = [name for name in self.canonical
                       if len(fold(name).split()) >= 2
                       and fold(name).split()[0] + " " + fold(name).split()[-1] == first_last]
            return matches[0] if len(matches) == 1 else None
        return None


@dataclass
class ItemVotes:
    item: Any
    motion_index: int = field(default=0, kw_only=True)
    method: str                                  # named | unanimous | tally
    outcome: Optional[str]                       # PASS | FAIL | None
    tally: Dict[str, int]
    member_votes: List[Tuple[str, str]] = field(default_factory=list)
    motion_text: str = ""
    offset: int = 0
    rung: str = ""
    observation_index: int = 0
    tally_basis: Optional[str] = None
    reported_body: Optional[str] = None   # A body credited with this action.


@dataclass
class Abstention:
    item: Any
    reasons: List[str]
    motion_text: str


@dataclass
class MeetingParse:
    attendance: Attendance
    published: List[ItemVotes] = field(default_factory=list)
    abstained: List[Abstention] = field(default_factory=list)
    items_anchored: int = 0
    items_total: int = 0
    evidence_seen: int = 0
    roster_source: str = "none"
    procedural_skipped: int = 0
    observations: List[Any] = field(default_factory=list)
    # (item_id, body) where an item's narrative credits another body without a
    # motion of its own here. A referral belongs to the matter's journey, not to
    # any motion in this meeting: crediting every motion in the block attributed
    # the council's own vote to the committee that had merely recommended.
    referrals: List[Tuple[str, str]] = field(default_factory=list)


# An agenda "item" that is really a section heading ("OLD BUSINESS:",
# "ITEMS SCHEDULED FOR VOTING SESSIONS") owns no motion; a vote landing on
# one is a misalignment, not a record.
# Approving the minutes or adopting the agenda is a real vote and a
# meaningless one; the item funnel already suppresses these as procedural,
# and the titles the chunker produces for them ("June 23, 2026 - Policy
# Meeting minutes") slip past the shared filter.
_PROCEDURAL_TITLE_RE = re.compile(
    r"\bminutes\b|\badopt(?:ion)?\s+(?:of\s+)?(?:the\s+)?agenda\b|\bapprov\w*\s+(?:of\s+)?(?:the\s+)?agenda\b"
    r"|\bconsent\s+agenda\b|\broll\s*call\b|\bcall\s+to\s+order\b|\badjourn",
    re.IGNORECASE,
)

_HEADING_RE = re.compile(
    r"^\s*(?:old|new|unfinished|other)\s+business\b|^\s*items?\s+(?:scheduled|for)\b"
    r"|^\s*(?:consent|regular|public\s+hearing|discussion|action|information)\s+(?:agenda|items?|calendar)\b"
    r"|^\s*(?:reports?|presentations?|proclamations?|communications?|announcements?)\s*:?\s*$",
    re.IGNORECASE,
)


def _is_heading(title: str) -> bool:
    stripped = (title or "").strip()
    if _HEADING_RE.search(stripped) or _PROCEDURAL_TITLE_RE.search(stripped):
        return True
    # A short all-caps line ending in a colon is a heading, not an item.
    return stripped.endswith(":") and len(stripped) < 60 and stripped == stripped.upper()


def _membership_ok(movers: Sequence[str], gazetteer: "Gazetteer", present: Sequence[str]) -> bool:
    """At least one named mover must be someone we are about to attribute to.

    Cheap and decisive: a packet holding several bodies' minutes anchors a
    committee motion under a council item, and the movers are the only names
    in the sentence that say which body acted. The check only runs when there
    is a roster to check against; with nothing to compare, it proves nothing
    and must not block (the named or unanimous gate still applies).
    """
    # Both sides must be canonical or neither matches: Apache Junction's roll call
    # prints bare surnames while the roster holds full names, so comparing a
    # resolved "Darryl Cross" against a printed "Cross" rejected every mover.
    known = {fold(gazetteer.resolve(n) or n) for n in (present or gazetteer.canonical)}
    if not known:
        return True
    candidates = [clean_name(m) for m in movers]
    candidates = [c for c in candidates if c and not _ROLE_ONLY_RE.fullmatch(c)]
    if not candidates:
        return True
    return any(fold(r) in known for r in (gazetteer.resolve(c) or "" for c in candidates))


# A mover captured as a bare office ("Council", "Floor Leader", "Chair")
# names nobody; it cannot confirm or deny membership.
_ROLE_ONLY_RE = re.compile(
    r"(?:council|board|commission|committee|floor\s+leader|chair|president|mayor|"
    r"clerk|staff|member|motion|second)\.?",
    re.IGNORECASE,
)


def parse_meeting(text: str, items: Sequence[Dict[str, Any]], roster: Sequence[str], dialect=None) -> MeetingParse:
    """Observe all evidence, then independently gate the public claims."""
    from parsing.rollcall.observations import observe_meeting
    return observe_meeting(text, items, roster, dialect)
