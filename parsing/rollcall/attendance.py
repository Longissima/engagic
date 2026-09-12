"""Attendance roster from the head of a minutes document.

For a city without a votes API the minutes are the only roster source. Every
template family observed prints one of three shapes near the top:

    Present  7 -  Flynn, Gonzales-Gutierrez, Vice Chair Amanda Sandoval, ...
    Absent  1 -  Romero Campbell
    Members present: Smith, Jones, Lee. Members absent: Kim.
    Mayor Moriwaki, Deputy Mayor Hytopoulos and Councilmembers Lant, Mathews,
    Nelson, and Schneider were present. Councilmember Fantroy-Johnson was absent

Confidence 7/10: the label forms are exact, the narrative form is a sentence
regex and is validated by the tally arithmetic downstream, never trusted on
its own.
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional

from parsing.rollcall.names import split_names

HEAD_CHARS = 6000

# A present-list often runs straight into the next roster on the same
# extracted line: "Members Present: A, B, C. Absent: D Guests attending: E".
_INLINE_CUT_RE = re.compile(
    r"\b(?:absent|excused|guests?|staff|also\s+present|others?\s+present|alternates?|"
    r"also\s+in\s+attendance|not\s+present)\b\s*:?",
    re.IGNORECASE,
)

# Words that mark a department, office or role rather than a person.
_NOT_PERSON_RE = re.compile(
    r"\b(?:parks?|recreation|departments?|dept|interns?|staff|city|county|planning|"
    r"directors?|clerks?|attorneys?|engineers?|managers?|works|public|finance|library|police|"
    r"fire|administrators?|administration|services|office|utilities|community|"
    r"development|building|zoning|secretar(?:y|ies)|recorders?|treasurers?|assistants?|"
    r"guests?|alternates?|liaisons?|consultants?|applicants?|"
    r"committees?|commissions?|boards?|affairs|items?|business|agenda|government|"
    r"adjourn\w*|minutes|hearings?|reports?|session)\b",
    re.IGNORECASE,
)

# A remote member is present: present for quorum, present for the vote. This
# label line was unknown, so the present list ran straight into it and fused the
# last present name to the first remote name -- "Ozog REMOTE Galassi" is two
# DuPage members and a participation mode stored as one person, which also made
# both surnames ambiguous and cost each of them their own identity.
_REMOTE_WORDS = (r"remote(?:ly)?|virtual(?:ly)?|telephonic(?:ally)?|teleconference|"
                 r"electronically|online")
_LABEL_RE = re.compile(
    r"^[ \t]*(?:members?\s+|board\s+members?\s+|councilmembers?\s+|those\s+)?"
    rf"(?P<kind>present|absent|excused|{_REMOTE_WORDS})\b\s*[:\-–]?\s*(?:(?P<count>\d+)\s*[-–]\s*)?(?P<rest>.*)$",
    re.IGNORECASE | re.MULTILINE,
)
_REMOTE_KIND_RE = re.compile(rf"^(?:{_REMOTE_WORDS})$", re.IGNORECASE)
_STOP_RE = re.compile(
    r"^[ \t]*(?:(?:members?\s+|also\s+)?(?:present|absent|excused|remote|virtual|telephonic|"
    r"teleconference|electronically|online|staff|also present|others present|"
    r"call to order|pledge|roll call|approval|agenda|minutes|action items|consent|briefings|\d+\.|[A-Z]\.)\b|\s*$)",
    re.IGNORECASE,
)
# Sentences wrap across extracted lines; allow newlines but never a period.
_NARRATIVE_PRESENT_RE = re.compile(
    r"(?P<names>[A-Z][^.:;]{3,400}?)\s+(?:were|was)\s+(?:all\s+)?present\b", re.DOTALL
)
_NARRATIVE_ABSENT_RE = re.compile(
    r"(?P<names>[A-Z][^.:;]{3,200}?)\s+(?:were|was)\s+absent\b", re.DOTALL
)
# "...Council Members were present: Jack Sheard, Mark Stelk, ... Absent: Mike Paulick."
# "Board members in attendance included Andrew Reynolds, Michael Telich and Brian LaFleur."
_NAMES_AFTER_PRESENT_RE = re.compile(
    r"(?:(?:were|was|members)\s+present|\bpresent)\s*(?:\bwere\b|\bwas\b|\bincluded\b|:)\s*"
    r"(?P<names>[^.]{5,400}?)(?:\.\s|\bAbsent\b|$)",
    re.DOTALL | re.IGNORECASE,
)
_IN_ATTENDANCE_RE = re.compile(
    r"in\s+attendance\s+(?:were|included|was)\s*:?\s*(?P<names>[^.]{5,400}?)\.", re.DOTALL | re.IGNORECASE
)
# "The following members ... were in attendance:" / "... were present:" -- the
# verb precedes the phrase and the roster follows as its own block of lines.
_ROSTER_INTRO_RE = re.compile(
    r"^[ \t]*(?:the\s+)?following\b[^:\n]{0,120}?\b(?:were|was|are|is)\s+"
    r"(?:in\s+attendance|present|as\s+follows)\s*:?[ \t]*$",
    re.IGNORECASE,
)
_INLINE_ABSENT_RE = re.compile(r"\bAbsent\s*:\s*(?P<names>[^.\n]{2,200}?)(?:\.|\n|$)", re.IGNORECASE)
# "Present were A, B" reaches the label path with the verb still attached.
_LEADING_VERB_RE = re.compile(r"^\s*(?:were|was|are|is|included|include)\b\s*:?\s*", re.IGNORECASE)
# A tabular roster carries an "Arrived" column. The timestamp ends one name line
# and glues to the next one when the lines are joined, so Waco's last member was
# rejected as a fragment containing a digit.
_COLUMN_NOISE_RE = re.compile(
    r"\s{2,}\d{1,2}:\d{2}\s*(?:[ap]\.?m\.?)?\s*$|\s{2,}\d{1,4}\s*$", re.IGNORECASE
)


@dataclass
class Attendance:
    present: List[str] = field(default_factory=list)
    absent: List[str] = field(default_factory=list)
    present_count: Optional[int] = None
    source: str = "none"
    # A roll-call line we could not read a name out of. The names we did get are
    # still worth having, but the list is no longer the whole body, so it must
    # not be used to decide that a mover sits outside it. A partial roster is
    # more dangerous than none: Cudahy's "Council / Agency Member Alcantar Loza"
    # left two of four members and that pair rejected every motion.
    partial: bool = False

    @property
    def known(self) -> bool:
        return bool(self.present)

    def drop_staff(self) -> None:
        """Remove role or department tokens that rode in on the same line."""
        self.present = [n for n in self.present if not _NOT_PERSON_RE.search(n)]
        self.absent = [n for n in self.absent if not _NOT_PERSON_RE.search(n)]

    def plausible(self) -> bool:
        return len(self.present) >= 2


# A vertical roster gives one name per line and no commas at all, so joining the
# lines produces a single unsplittable blob and the whole list is lost. Cerritos,
# Kane County and Waco all print this shape. Reading the lines as candidates in
# their own right recovers them, and the cap can then be generous because each
# line stands or falls on its own.
COLLECT_LINES = 14


def _as_names(collected: List[str]) -> List[str]:
    """Names from a collected block, read per line only when it has no delimiters.

    The delimiter decides, not the count. A comma-separated roster must be read
    joined, because a name wraps across the line break ("Councilor Gina" /
    "Franzosa (online)") and reading lines apart splits that person in two.
    A block with no commas at all is one name per line, and joining it yields a
    single unsplittable blob -- more per-line names there would be a false win.
    """
    body = [part for part in collected if part]
    if body and not any("," in part or ";" in part for part in body):
        names: List[str] = []
        for line in body:
            for candidate in split_names(line):
                if candidate not in names:
                    names.append(candidate)
        return names
    return split_names(" ".join(body))


def _collect(lines: List[str], start: int) -> List[str]:
    """Names continue on following lines until a blank or a new label.

    A wrapped tail ("Stephanie" / "W. Telles") looks like a lettered heading
    to the stop rule; a real heading carries a sentence, a wrapped name does
    not, so a line of three words or fewer after an unterminated line stays.
    """
    out = [lines[start]]
    for line in lines[start + 1:start + COLLECT_LINES]:
        stripped = line.strip()
        # A digit anywhere used to end the collection, which killed every
        # tabular roster: "Andrea Barefield, Council Member, District 1" and an
        # "Arrived 2:10 PM" column both carry digits and are both name lines.
        # Only a leading enumerator marks a heading; split_names already drops
        # any fragment containing a digit.
        heading = re.match(r"^(?:[a-z]|\d+(?:\.\d+)*|[A-Z]{2,3})[.)]\s|^\d+[.)]?\s", stripped)
        # A line that is itself a category label is never a wrapped name tail,
        # however short it is. Without this, "REMOTE  Galassi" read as the
        # continuation of the absent list and fused two members into one name.
        wrapped_tail = (
            len(stripped.split()) <= 3 and not heading
            and not _LABEL_RE.match(line) and not out[-1].rstrip().endswith(".")
        )
        if not stripped or (_STOP_RE.match(line) and not wrapped_tail) or (heading and not wrapped_tail):
            break
        out.append(stripped)
    return out


def parse_attendance(text: str) -> Attendance:
    head = text[:HEAD_CHARS]
    lines = head.splitlines()
    result = Attendance()
    for i, line in enumerate(lines):
        m = _LABEL_RE.match(line)
        if not m:
            continue
        rest = m.group("rest").strip()
        if rest and rest[0] in ".,:" and len(rest) < 3:
            continue
        collected = (_collect([rest] + lines[i + 1:], 0) if rest
                     else _collect(lines, i + 1) if i + 1 < len(lines) else [])
        trimmed = []
        for part in collected:
            part = _COLUMN_NOISE_RE.sub("", part)
            part = _LEADING_VERB_RE.sub("", part)
            cut = _INLINE_CUT_RE.search(part)
            if cut and cut.start() > 0:
                part = part[:cut.start()]
            trimmed.append(part)
            if cut:
                break
        names = _as_names(trimmed)
        unread = sum(1 for part in trimmed if part.strip() and not split_names(part))
        kind = m.group("kind").lower()
        if kind == "present" and names and not result.present:
            result.present = names
            result.present_count = int(m.group("count")) if m.group("count") else None
            result.source = "label"
            result.partial = bool(unread)
        elif _REMOTE_KIND_RE.match(kind) and names and result.present:
            result.present += [n for n in names if n not in result.present]
        elif kind in ("absent", "excused") and names and not result.absent:
            result.absent = names
    if not result.present:
        for i, line in enumerate(lines):
            if not _ROSTER_INTRO_RE.match(line):
                continue
            start = next((j for j in range(i + 1, min(i + 4, len(lines)))
                          if lines[j].strip()), None)
            if start is None:
                continue
            names = _as_names(_collect(lines, start))
            if len(names) >= 2:
                result.present = names
                result.source = "label"
                break
    if not result.present:
        for pattern in (_NAMES_AFTER_PRESENT_RE, _IN_ATTENDANCE_RE, _NARRATIVE_PRESENT_RE):
            m = pattern.search(head[:4000])
            if not m:
                continue
            blob = m.group("names").replace("\n", " ")
            cut = _INLINE_CUT_RE.search(blob)
            if cut and cut.start() > 0:
                blob = blob[:cut.start()]
            names = split_names(blob)
            if 2 <= len(names) <= 25:
                result.present = names
                result.source = "narrative"
                break
    result.drop_staff()
    if result.present_count is not None and len(result.present) != result.present_count:
        result.partial = True
    if result.present and not result.plausible():
        result = Attendance()
    if result.present and not result.absent:
        a = _INLINE_ABSENT_RE.search(head[:4000]) or _NARRATIVE_ABSENT_RE.search(head[:4000])
        if a:
            blob = a.group("names").replace("\n", " ")
            cut = _INLINE_CUT_RE.search(blob)
            if cut and cut.start() > 0:
                blob = blob[:cut.start()]
            result.absent = split_names(blob)[-4:]
            result.drop_staff()
    return result
