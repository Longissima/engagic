"""Generic minutes engine: items in, attributed votes out, or nothing.

    attendance = parse_attendance(text)
    blocks     = align(text, items)
    for block:  evidence = find_evidence(block)
                publish the last evidence per item through the gate

Attribution tiers, in order of what the document supports:
  named      the clerk listed names per category; every name must resolve
             uniquely against roster + attendance, each member at most once,
             list sizes must equal stated counts and the tally when printed
  unanimous  a tally with zero dissent whose yes-count equals the attendance
             present-count: everyone present voted aye, the absent are absent
  tally      outcome and counts only; no per-member rows

Anything the arithmetic cannot confirm is abstained with a reason, never
guessed. Confidence 7/10 overall; the named tier is the spike's gate.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from parsing.rollcall.align import anchor_items, blocks
from parsing.rollcall.attendance import Attendance, parse_attendance
from parsing.rollcall.evidence import Evidence, find_evidence
from parsing.rollcall.names import clean_name, fold
from pipeline.filters import get_filter_decision


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
        if len(parts) >= 2 and " ".join(parts[-2:]) in self.keys:
            return self.keys[" ".join(parts[-2:])]
        if parts and parts[-1] in self.keys:
            return self.keys[parts[-1]]
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
    known = {fold(n) for n in (present or gazetteer.canonical)}
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


def _dedupe(names: Sequence[str]) -> List[str]:
    """One row per member: a name repeated in an attendance line is one person."""
    seen: Dict[str, str] = {}
    for name in names:
        if name:
            seen.setdefault(fold(name), name)
    return list(seen.values())


def _tally_from_sections(ev: Evidence) -> Dict[str, int]:
    tally = {"yes": 0, "no": 0, "abstain": 0, "absent": 0, "present": 0}
    for s in ev.sections:
        n = len(s.names) if s.names else (s.stated or 0)
        key = {"AYE": "yes", "NO": "no", "ABSTAIN": "abstain", "RECUSED": "abstain",
               "ABSENT": "absent", "EXCUSED": "absent", "PRESENT": "present", "NONVOTING": "present"}[s.value]
        tally[key] += n
    return tally


def _gate_named(ev: Evidence, gazetteer: Gazetteer) -> Tuple[List[Tuple[str, str]], List[str]]:
    reasons: List[str] = []
    votes: List[Tuple[str, str]] = []
    seen: Dict[str, str] = {}
    for section in ev.sections:
        if section.stated is not None and section.names and len(section.names) != section.stated:
            reasons.append(f"tally: {section.value} extracted {len(section.names)} names, stated {section.stated}")
        for raw in section.names:
            full = gazetteer.resolve(raw)
            if full is None:
                reasons.append(f"unresolved: {raw}")
                continue
            if full in seen:
                reasons.append(f"duplicate: {full} in {seen[full]} and {section.value}")
                continue
            seen[full] = section.value
            votes.append((full, section.value))
    if ev.tally:
        yes = sum(1 for _, v in votes if v == "AYE")
        no = sum(1 for _, v in votes if v == "NO")
        if (yes, no) != (ev.tally[0], ev.tally[1]):
            reasons.append(f"tally: lists {yes}-{no} vs printed {ev.tally[0]}-{ev.tally[1]}")
    if not votes:
        reasons.append("empty: no member votes extracted")
    return votes, reasons


def _self_consistent_names(evidence_by_block: List[List[Evidence]]) -> List[str]:
    """Names to trust when the document is the only roster source.

    A name counts when it appears in a named list whose size matches its
    stated count, and it recurs in at least two such lists in the document.
    A clerk's one-off typo cannot recur; a real member votes more than once.
    """
    seen: Dict[str, int] = {}
    for evidence in evidence_by_block:
        for ev in evidence:
            for section in ev.sections:
                if not section.names or (section.stated is not None and section.stated != len(section.names)):
                    continue
                for raw in section.names:
                    key = fold(clean_name(raw))
                    seen[key] = seen.get(key, 0) + 1
                    seen.setdefault("display:" + key, 0)
    counts = {k: v for k, v in seen.items() if not k.startswith("display:")}
    trusted: Dict[str, str] = {}
    for evidence in evidence_by_block:
        for ev in evidence:
            for section in ev.sections:
                for raw in section.names:
                    key = fold(clean_name(raw))
                    if counts.get(key, 0) >= 2:
                        trusted.setdefault(key, clean_name(raw))
    return sorted(trusted.values())


def parse_meeting(text: str, items: Sequence[Dict[str, Any]], roster: Sequence[str]) -> MeetingParse:
    attendance = parse_attendance(text)
    result = MeetingParse(attendance=attendance, items_total=len(items))

    anchors = anchor_items(text, items)
    result.items_anchored = len(anchors)
    item_blocks = blocks(text, anchors)
    evidence_by_block = [find_evidence(text[b["start"]:b["end"]]) for b in item_blocks]

    names = list(roster) + attendance.present + attendance.absent
    if not names:
        names = _self_consistent_names(evidence_by_block)
        result.roster_source = "named_lists" if names else "none"
    else:
        result.roster_source = "roster" if roster else attendance.source
    gazetteer = Gazetteer(names)
    present = _dedupe([gazetteer.resolve(n) or clean_name(n) for n in attendance.present])
    absent = [n for n in _dedupe([gazetteer.resolve(n) or clean_name(n) for n in attendance.absent])
              if n not in present]

    for block, evidence in zip(item_blocks, evidence_by_block):
        if not evidence:
            continue
        result.evidence_seen += len(evidence)
        # Approving the minutes or the agenda is not an accountability fact;
        # the same filter that keeps these items out of summaries applies.
        title = str(block["item"].get("title") or "")
        if get_filter_decision(title) or _is_heading(title):
            result.procedural_skipped += 1
            continue
        # Every motion on the item, in document order. An item can be amended
        # and then adopted, and keeping only the disposition erased the vote
        # on the amendment -- often the only one anybody disagreed on.
        motion_index = 0
        for ev in evidence:
            published_before = len(result.published)
            _publish_one(
                ev, block, motion_index, gazetteer, present, absent, result
            )
            if len(result.published) > published_before:
                motion_index += 1
    return result

def _publish_one(ev, block, motion_index, gazetteer, present, absent, result) -> None:
    """Evaluate one motion and append its outcome, or an abstention."""
    if ev.procedural:
        # A motion to adjourn or reconvene rides at the end of whatever
        # block it fell in; it is not a vote on that item.
        result.procedural_skipped += 1
        return
    if not _membership_ok(ev.movers, gazetteer, present):
        result.abstained.append(Abstention(
            item=block["item"],
            reasons=[f"membership: mover not on this roster ({ev.movers[:2]})"],
            motion_text=ev.result_text,
        ))
        return
    motion_text = ev.result_text
    offset = block["start"] + ev.offset

    if ev.named:
        votes, reasons = _gate_named(ev, gazetteer)
        if reasons:
            result.abstained.append(Abstention(item=block["item"], reasons=reasons, motion_text=motion_text))
            return
        tally = _tally_from_sections(ev)
        outcome = ev.outcome or ("PASS" if tally["yes"] > tally["no"] else "FAIL")
        result.published.append(ItemVotes(
            item=block["item"], method="named", outcome=outcome, tally=tally,
            member_votes=votes, motion_text=motion_text, offset=offset, rung=block["rung"],
            motion_index=motion_index,
        ))
        return

    if ev.tally:
        yes, no, third = ev.tally
        tally = {"yes": yes, "no": no, "abstain": third, "absent": len(absent), "present": 0}
        outcome = ev.outcome or ("PASS" if yes > no else "FAIL")
        if no == 0 and third == 0 and present and len(present) == yes:
            member_votes = [(n, "AYE") for n in present] + [(n, "ABSENT") for n in absent]
            result.published.append(ItemVotes(
                item=block["item"], method="unanimous", outcome=outcome, tally=tally,
                member_votes=member_votes, motion_text=motion_text, offset=offset, rung=block["rung"],
                motion_index=motion_index,
            ))
        else:
            result.published.append(ItemVotes(
                item=block["item"], method="tally", outcome=outcome, tally=tally,
                motion_text=motion_text, offset=offset, rung=block["rung"],
                motion_index=motion_index,
            ))
        return

    if ev.unanimous and ev.outcome and present:
        tally = {"yes": len(present), "no": 0, "abstain": 0, "absent": len(absent), "present": 0}
        result.published.append(ItemVotes(
            item=block["item"], method="unanimous", outcome=ev.outcome, tally=tally,
            member_votes=[(n, "AYE") for n in present] + [(n, "ABSENT") for n in absent],
            motion_text=motion_text, offset=offset, rung=block["rung"],
            motion_index=motion_index,
        ))
        return

    if ev.outcome:
        result.published.append(ItemVotes(
            item=block["item"], method="outcome", outcome=ev.outcome,
            tally={}, motion_text=motion_text, offset=offset, rung=block["rung"],
            motion_index=motion_index,
        ))
        return
    result.abstained.append(Abstention(item=block["item"], reasons=["no outcome or tally"], motion_text=motion_text))
