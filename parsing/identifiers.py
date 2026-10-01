"""Durable identifier extraction from agenda text.

Legislative bodies act on the same thing repeatedly: a contract goes to
committee, then to the full council, then comes back as an amendment. Vendors
rarely expose a stable key for that -- many mint a fresh backend id per agenda --
but the government's own text almost always cites one: a contract number, a law
department file number, a zoning case number.

Extracting that identifier turns a pile of unrelated items into one matter with
one canonical summary and a real timeline.

A wrong identifier is expensive and not self-correcting: it permanently merges
unrelated items into one city_matters row under a single summary. Every pattern
here is therefore anchored on an explicit label and refuses to guess from bare
numbers. Validated against 30 real agendas across 14 cities: 110 matches, zero
false positives.
"""

import re
from datetime import date, datetime
from typing import List, NamedTuple, Optional, Sequence, Tuple, Union


class Identifier(NamedTuple):
    """A matter's durable identity as printed by the government.

    `file` is the namespaced number ("Bill 66", "Contract 6007968"). `year` is
    the numbering period when the number alone is not unique: a year printed
    beside it ("Bill No. 65, 2024"), or the run the city's own numbering puts
    it in (see detect_numbering_runs). generate_matter_id hashes the year
    in only when it is present, so every identifier that never needed one
    keeps the matter id it already has.
    """

    file: str
    type: Optional[str]
    year: Optional[str] = None

# (class label, matter_type, pattern). Order is priority order.
#
# The emitted matter_file is namespaced by class ("Contract 6007968", not
# "6007968") because a contract number and a court case number can collide
# numerically inside one city.
IDENTIFIER_PATTERNS: List[Tuple[str, str, str]] = [
    # Contract No. 6007968 / 6006718-A1 (AMEND 1) / 6007823-D / 6007381-R.
    #
    # Two guards, both learned from real corpus damage:
    #   - The suffix group must consume at least one character. An
    #     optional-content group happily captures a dangling hyphen
    #     ("Contract 210044-"), and "6006718-100% City Funding" captures a
    #     funding share as an amendment suffix.
    #   - "Master Contract" is skipped. Alameda County writes "Master Contract
    #     No. 902683; Procurement Contract No. 30090" -- the master is a
    #     vendor-level umbrella, so keying on it would merge every distinct
    #     agreement with that vendor into a single matter. The procurement
    #     number in the same sentence is the item's actual identity.
    ("Contract", "Contract",
     r"\bContract\s*(?:No\.?|Number|#)\s*"
     r"([0-9]{4,10}(?:-(?!\d{1,3}%)[A-Za-z0-9]{1,4})?)\b"),
    # File No. L25-8029 (Detroit law dept) / File #SD25-0033 (Los Altos Hills site
    # development) / File No. CM25-19446 (Tampa) / File No. 15120 (workers' comp).
    # The type stays generic: cities file lawsuits, permits and rezonings alike
    # under "File No.".
    # "CASE FILE NO. 2026-07-V" (Odessa) is one file, not "File 2026": a bare
    # number that continues into a dashed code is never the whole identifier.
    ("File", "File",
     r"\bFile\s*(?:No\.?|#)\s*"
     r"([A-Za-z]{0,2}[0-9]{2}-[0-9]{3,6}|[0-9]{4}-[0-9]{2,3}-[A-Za-z0-9]{1,5}|[0-9]{4,6}(?!-[A-Za-z0-9]))\b"),
    # Unlabelled law-department file cited mid-sentence: "...24-016365-NF; L24-01403 (VI)".
    # Same settlement, same durable handle, so it must key identically to the
    # sibling items that do label it -- otherwise one lawsuit becomes two matters.
    ("File", "Settlement", r"\b(L[0-9]{2}-[0-9]{3,6})\b"),
    # Case No. 25-011182 / Case #26-041 / Case No. 25-cv-10245 / Case No. W24-00043
    ("Case", "Case",
     r"\bCase\.?\s*(?:No\.?|#)\s*([A-Za-z]{0,2}[0-9]{2}-(?:[A-Za-z]{2}-)?[0-9]{3,6})\b"),
    # Petition No. 1234 (street vacations, encroachments)
    ("Petition", "Petition",
     r"\bPetition\s*(?:No\.?|Number|#)\s*([0-9]{3,7}(?:-[A-Za-z0-9]{1,3})?)\b"),
]

_COMPILED = [
    (label, matter_type, re.compile(pattern, re.IGNORECASE))
    for label, matter_type, pattern in IDENTIFIER_PATTERNS
]

# Legislative instrument numbers. These are searched only in the title and the
# head of the body: an ordinance body routinely cites the ordinances it amends
# ("...amending Ordinance No. 1187..."), and keying on a cited number would
# merge the amendment into the thing it amends. The head of an item is where
# the instrument names itself.
#
# Token shape: optional letter prefix, digits, up to two dashed suffixes
# ("2026-05", "1234", "O-26-12", "RS2026-118", "25-08-A"). A bare year with no
# dash ("Ordinance No. 2026") is rejected as a probable typo or date.
# Dotted numbers are real instrument numbers in some cities (Parker CO
# "Ordinance No. 3.372.8"); so are three dashed parts (Amarillo "Resolution
# No. 03-10-26-2") and a colon sequence (Paramount "Resolution26:021"). A number running on into "/digits" or ".digits"
# is a code section or a compound file ("Ordinance 03/0326001/26"), not
# this token.
_INSTRUMENT_TOKEN = (
    r"([A-Za-z]{0,4}-?(?:[0-9]{1,2},[0-9]{3}|[0-9]{1,6}(?:\.[0-9]{1,4}){0,3})"
    r"(?:-[A-Za-z0-9.]{1,6}){0,3}(?::[0-9]{1,6})?)\b"
    r"(?![./][0-9])"
)
# The "No." is optional: CivicClerk-era titles write "Resolution 76-26,",
# "Adopt Ordinance 1719 on First Reading", "RESOLUTION 4302 - ...". Safe only
# because these patterns read the head alone and pass the citation, bare-year
# and placeholder guards.
_INSTRUMENT_LABEL = r"\s*(?:(?:No\.?|Number|#)\s*:?\s*|\s|(?=[0-9]))"
# A cited instrument is not the item's own: "amending Ordinance No. 1187",
# "per CA Assembly Bill No. 481". Matches preceded by these are skipped.
# The second group is boilerplate that cites an instrument as its authority:
# "pursuant to District Ordinance No. 12" (AC Transit's device notice merged
# 169 items over three years), "in accordance with Policy Resolution No. 10"
# (Napa), "in compliance with Ordinance #1868" (New Haven), "from Ordinance
# No. 2926" (Bradenton). Measured 2026-10-01.
_CITATION_BEFORE_RE = re.compile(
    r"(?:amend(?:ing|ed|s)?\b|amendments?\s+(?:to|of)\b|repeal\w*|rescind\w*|supersed\w*|assembly|senate|house"
    r"|pursuant\s+to|accordance\s+with|compliance\s+with|conformance\s+with|consistent\s+with"
    r"|required\s+by|authorized\s+by|established\s+by|adopted\s+by|approved\s+by|created\s+by"
    r"|governed\s+by|set\s+forth\s+in|defined\s+in|described\s+in|provided\s+(?:in|by)"
    r"|associated\s+with|in\s+connection\s+with|codified\s+(?:as|in|at|by)"
    r"|under|per|from)"
    r"\s+(?:\w+\s+){0,2}$",
    re.IGNORECASE,
)
HEAD_IDENTIFIER_PATTERNS: List[Tuple[str, str, str]] = [
    # "Ord"/"Res" abbreviations: Garland "GDC Amendment ORD 26-02", Juneau "Ord 2026-...".
    ("Ordinance", "Ordinance", r"\b(?:Ordinance|Ord\b\.?)" + _INSTRUMENT_LABEL + _INSTRUMENT_TOKEN),
    ("Resolution", "Resolution", r"\b(?:Resolution|Res\b\.?)" + _INSTRUMENT_LABEL + _INSTRUMENT_TOKEN),
    # "Board Bill 107" (St. Louis), "Council Bill 120345" (Seattle), "Bill No. 25-123".
    ("Bill", "Bill",
     r"\b(?:(?:Board|Council)\s+Bill|Bill\s*(?:No\.?|Number|#))\s*:?\s*" + _INSTRUMENT_TOKEN),
]
_HEAD_COMPILED = [
    (label, matter_type, re.compile(pattern, re.IGNORECASE))
    for label, matter_type, pattern in HEAD_IDENTIFIER_PATTERNS
]
HEAD_CHARS = 300
# X runs stay attached so "2026-14XX" still reads as a placeholder.
_FUSED_NUMBER_RE = re.compile(r"([0-9])((?!X+\b)[A-Z]{4,}|[A-Z][a-z])")
_FUSED_WORD_RE = re.compile(r"([a-z])([A-Z])")
_DEFERS_TO_RE = re.compile(
    r"\s*,?\s+for\s+(?:substitute\s+)?(?:board\s+|council\s+)?(?:bill|ordinance|resolution)\b",
    re.IGNORECASE,
)
_LIST_JOIN_RE = re.compile(r"[\s,;&]*(?:and|or)?[\s,;&]*", re.IGNORECASE)
# "PROPOSED ORDINANCE NO. 6 - 26" (Pensacola) is the number 6-26.
# Only a standalone number joined to a complete 2-4 digit tail: "2026-10 - 2nd
# Quarter" and "2026-0914 - 1 An Ordinance" stay as they are.
_SPACED_DASH_RE = re.compile(r"(?<![0-9-])([0-9]{1,4})\s+[-\u2013]\s+([0-9]{2,4})(?![A-Za-z0-9])")

# Unlabelled codes that are only trusted in the title, where a chunker or
# vendor put the document's own heading. Emitted raw (no class prefix): the
# prefix letters are the namespace, and API vendors store the same shape
# ("BL2025-1098") unprefixed, so a city migrating vendors keys identically.
#
# Leading legislative file token: "2026-412 Approve..." / "24-0123 Ordinance...".
# 4-digit-year or 2-digit-year prefix only, so "03-25" (a date) never matches.
# An alphanumeric sub-file suffix is part of the identity: Los Angeles files
# every appointment under "22-1200-S49", and the S-numbers are distinct items.
_LEADING_FILE_RE = re.compile(
    r"^\s*((?:(?:19|20)\d{2}-\d{1,6}|\d{2}-\d{3,6})(?:-[A-Z]{1,2}\d{1,4})?)(?![0-9])(?!-\d)"
)
_WORD_RE = re.compile(r"[A-Za-z]{4}")
# Parenthesised department code: "(PC-11015)" / "(GEN-1284)" (Oklahoma City).
_PAREN_CODE_RE = re.compile(r"\(([A-Z]{1,5}-[0-9]{3,6})\)")
# Prefixed year-numbered code: "CU-26-012", "RES-2026-14", "PUD-24-007".
# The middle group must be year-shaped so "US-101-..." style names never
# match; two-digit years are limited to 2015-2029 so "CC-06-15" (a date-like
# committee code) does not key.
# Year-last form: "O-079-26" / "R-112-26" (Louisville ordinances and resolutions).
_DASH_CODE_RE = re.compile(
    r"\b([A-Z]{2,5}-(?:1[5-9]|2[0-9]|(?:19|20)[0-9]{2})-[0-9]{2,6}(?:-[0-9]{1,6}){0,2}"
    r"|[A-Z]{1,5}-[0-9]{3,4}-(?:1[5-9]|2[0-9]))\b"
)


# A number not yet assigned: "Resolution No. 2026-XXX", "R-26-XXX", "Ord
# 2026-_____", "Ordinance No. O-26-___", "Resolution 2026-0XX". Keying on one
# merges every draft of that type in the city (Santa Ana: 46 items under
# "Resolution 2026-XXX"; Schaumburg: 50 under "Resolution R-26").
_PLACEHOLDER_SEGMENT_RE = re.compile(r"(?:^|-)(?:[Xx]+|[0-9]*[Xx]{2,}[0-9]*|_+)(?=-|$)")
# A dash followed by nothing is punctuation ("Contract No. 210044- see
# attached"), so only an underscore or X run after the token marks a blank.
_BLANK_AFTER_RE = re.compile(r"^(?:-?\s?_|-[Xx]{2,})")
# A year prefix with nothing after its dash is a blank, not punctuation:
# Hillsborough prints "RESOLUTION NO. 26- " on every draft resolution.
_YEAR_PREFIX_RE = re.compile(r"[A-Za-z]{0,4}-?(?:[0-9]{2}|(?:19|20)[0-9]{2})")
_DANGLING_DASH_RE = re.compile(r"^-(?=\s|$|[.,;:)])")


def is_placeholder_token(token: str, text_after: str = "") -> bool:
    """True when the token is a number still to be assigned, not an identity."""
    if _PLACEHOLDER_SEGMENT_RE.search(token) or "XX" in token.upper():
        return True
    if _DANGLING_DASH_RE.match(text_after) and _YEAR_PREFIX_RE.fullmatch(token):
        return True
    return bool(_BLANK_AFTER_RE.match(text_after))


# A year printed beside a bare instrument number: "BILL NO. 106, 2026" (St.
# Louis County), "RESOLUTION NO. 100, 2026" (Scranton), "Ordinance No. 198 of
# 2025" (New Rochelle), "Ordinance No. 1 Series 2026" (Louisville),
# "Resolution No. 681 (2026)".
_PRINTED_YEAR_RE = re.compile(
    r"^(?:\s*,\s*|\s+of\s+|\s+series\s+(?:of\s+)?|\s*\(\s*)((?:19|20)[0-9]{2})(?![0-9])",
    re.IGNORECASE,
)
# Only a bare number needs a period to be unique; "2026-14", "55-26" and
# "O-079-26" already carry theirs.
_BARE_NUMBER_RE = re.compile(r"[A-Za-z]{0,4}-?[0-9]{1,6}")

# Numbering runs: no per-city configuration. Some councils restart their
# numbers each year or session and never print the period (St. Louis City's
# Board Bills restart every April), most never restart. Each city's own
# numbers say which: a run is a stretch of climbing numbers, and a reset is
# the numbers falling back to the bottom and climbing again. Detection runs
# offline over a city's full history (scripts/rekey_matters.py writes
# numbering_runs); the sync funnel only reads it. A city that never resets
# has one run, its numbers get no period, and its matter ids never change.
RUN_MIN_HIGH = 20          # a run must reach this before a fall counts as a reset
RUN_WINDOW_DAYS = 120      # the new run must prove itself within this window
RUN_MIN_NEW_NUMBERS = 10   # distinct low numbers the new run must show
RUN_MAX_OLD_SHARE = 0.2    # old-range numbers allowed in the window (carryovers)
CARRYOVER_SLACK = 10       # a number this far past the run's high is a carryover
SIBLING_WINDOW_DAYS = 200  # how far a printed year reaches to an unprinted mention


def detect_numbering_runs(occurrences: List[Tuple[date, int]]) -> List[date]:
    """Start dates of every run after the first, from (meeting date, number).

    Pass only bare numbers with no printed year: a printed year already names
    its period, and a city that prints years carries several at once (St.
    Louis County agendas list 2024, 2025 and 2026 bills together). Validated
    2026-10-01 over the 56 series with enough data: one reset found, St. Louis
    Board Bills on 2026-05-04, and none anywhere else. Confidence 7/10.
    """
    ordered = sorted(occurrences)
    starts: List[date] = []
    high = previous_high = 0
    for index, (day, number) in enumerate(ordered):
        if high >= RUN_MIN_HIGH and number <= max(3, high // 10):
            window = [n for d, n in ordered[index:] if (d - day).days <= RUN_WINDOW_DAYS]
            new_low = {n for n in window if n <= high // 2}
            old_range = sum(1 for n in window if n > high // 2)
            if len(new_low) >= RUN_MIN_NEW_NUMBERS and old_range < RUN_MAX_OLD_SHARE * len(window):
                starts.append(day)
                previous_high, high = high, number
                continue
        # A previous run's bill heard after the reset is a carryover; letting it
        # raise the new run's high would make the next low number a false reset.
        if previous_high and number > high + CARRYOVER_SLACK and number > previous_high // 2:
            continue
        high = max(high, number)
        if previous_high and high > previous_high // 2:
            previous_high = 0  # the new run has climbed into the old range
    return starts


def numbering_period(
    identifier: Identifier,
    meeting_date: Optional[Union[date, datetime]],
    run_starts: Sequence[date] = (),
    run_high: Optional[int] = None,
    printed_sibling: Optional[str] = None,
) -> Optional[str]:
    """Period that disambiguates a bare instrument number, or None.

    A printed year wins: it is the government's own statement. Otherwise the
    number belongs to the latest run that started on or before the meeting,
    named by the year it started, unless it sits far past that run's high
    mark so far (`run_high`, the caller's lookup): then it is a carryover from
    the previous run, like a December bill heard after a January reset. The
    first run is unnamed, which is what keeps every non-restarting city's ids
    exactly as they were.

    `printed_sibling` is the year the city printed beside this same number
    nearest in time (the caller's lookup, within SIBLING_WINDOW_DAYS): a city
    that prints "Bill No. 86, 2026" in one place means the same period when a
    later agenda just says "Bill No. 86".
    """
    series, _, number = identifier.file.partition(" ")
    if not number or not _BARE_NUMBER_RE.fullmatch(number):
        return None
    if identifier.year:
        return identifier.year
    if printed_sibling:
        return printed_sibling
    if meeting_date is None:
        return None
    day = meeting_date.date() if isinstance(meeting_date, datetime) else meeting_date
    started = [start for start in sorted(run_starts) if start <= day]
    if not started:
        return None
    digits = re.sub(r"[^0-9]", "", number)
    if run_high is not None and digits and int(digits) > run_high + CARRYOVER_SLACK:
        started = started[:-1]
    return str(started[-1].year) if started else None


_IDENTIFIER_WORD_RE = re.compile(r"[A-Za-z]{4,}")


def canonical_matter_file(raw: Optional[str]) -> Optional[str]:
    """Validate a matter file supplied by a vendor or adapter, or None.

    Every matter file passes through here before it becomes identity, whoever
    produced it. Rejected: no digit at all (PrimeGov "-----", IQM2
    "RESOLUTION" / "Proclamation"), an unassigned placeholder ("2026-XX"), and
    title fragments that an adapter split off a title ("Recommendation to go
    into Closure at 5"). A rejected file falls through to the text extractor
    and then to the vendor's own matter id.
    """
    if not raw:
        return None
    value = re.sub(r"\s+", " ", raw).strip()
    if not value or not re.search(r"[0-9]", value) or len(value) > 40:
        return None
    if len(_IDENTIFIER_WORD_RE.findall(value)) >= 3:
        return None
    if is_placeholder_token(value.split(" ")[-1]):
        return None
    return value


def extract_leading_file_token(title: Optional[str]) -> Optional[str]:
    """Leading legislative file number from an item title, or None.

    The remainder must carry a real word (bare numerics never link), and a
    year + valid MMDD shape ("2026-0615") is treated as a date, not a file.
    """
    m = _LEADING_FILE_RE.match(title or "")
    if not m:
        return None
    token = m.group(1)
    if not _WORD_RE.search((title or "")[m.end():]):
        return None
    first, _, second = token.partition("-")
    if len(first) == 4:
        # "2026-2027 Budget" / "2027-28 Work Plan" are fiscal-year ranges.
        if len(second) == 4 and second.startswith(("19", "20")):
            return None
        if len(second) == 2 and 0 < int(second) - int(first) % 100 <= 10:
            return None
        if len(second) == 4:
            mm, dd = int(second[:2]), int(second[2:])
            if 1 <= mm <= 12 and 1 <= dd <= 31:
                return None
    return token


def _is_bare_year(token: str) -> bool:
    """A plausible calendar year ("Ordinance No. 2026", a typo or a date).
    Years past next year are instrument numbers: Crestview's Ordinance 2034."""
    return token.isdigit() and len(token) == 4 and 1950 <= int(token) <= date.today().year + 1


def extract_identifier(*texts: Optional[str]) -> Optional[Identifier]:
    """Return the Identifier for the first labelled identifier found.

    Texts are searched as one document in the order given, so callers should pass
    the most authoritative source first (title before body).

    Amendment suffixes are preserved: 6006718-A1 is a distinct council action from
    6006718, and merging them would blur an award into its amendment under one
    canonical summary.
    """
    haystack = "\n".join(text for text in texts if text)
    if not haystack:
        return None

    title = texts[0] if texts else None
    body = "\n".join(text for text in texts[1:] if text)

    # Precedence, confidence 8/10: labelled durable handles anywhere in the text
    # (contract, file, case, petition) outrank instrument numbers, which
    # outrank unlabelled title codes. A resolution approving a contract keys on
    # the contract, since that is what recurs across committee, council and
    # amendment.
    for label, matter_type, pattern in _COMPILED:
        for match in pattern.finditer(haystack):
            # ``Master`` is a descriptor, not a stable action identity. Check
            # the actual whitespace before each match rather than encoding two
            # fixed-width lookbehinds: title/body text may contain tabs or
            # multiple spaces, and a false positive would merge every child
            # agreement beneath one umbrella matter.
            if label == "Contract" and re.search(
                r"master\s+$", haystack[:match.start()], re.IGNORECASE
            ):
                continue
            if is_placeholder_token(match.group(1), haystack[match.end():match.end() + 4]):
                continue
            return Identifier(f"{label} {match.group(1).upper()}", matter_type)

    # The title is capped too: some vendors put a whole notice in it (AC
    # Transit's "MEETING DISCLOSURES" blob cites two district ordinances).
    head = "\n".join(text for text in ((title or "")[:HEAD_CHARS], body[:HEAD_CHARS]) if text)
    # HTML flattened without separators fuses a number to the next word
    # ("Board Bill Number 1Introduced by ..."; every CivicClerk St. Louis
    # title), which defeats the token's word boundary.
    head = _FUSED_WORD_RE.sub(r"\1 \2", _FUSED_NUMBER_RE.sub(r"\1 \2", head))
    head = _SPACED_DASH_RE.sub(r"\1-\2", head)
    for label, matter_type, pattern in _HEAD_COMPILED:
        cited_end = -1
        for match in pattern.finditer(head):
            token = match.group(1)
            if _is_bare_year(token):
                continue
            if is_placeholder_token(token, head[match.end():match.end() + 4]):
                continue
            # Unlabelled and one digit is a list position ("Ordinance 1" under
            # an Ordinances heading, Ardmore), not an instrument number.
            if len(token) == 1 and not re.search(r"(?:No\.?|Number|#)\s*:?\s*$", match.group(0)[: -len(token)], re.I):
                continue
            # A list continues the citation that opened it: "in accordance
            # with Ordinance No. 6161, Ordinance No. 6491 and ..." (Hillsboro).
            continues_citation = cited_end >= 0 and _LIST_JOIN_RE.fullmatch(head[cited_end:match.start()])
            if continues_citation or _CITATION_BEFORE_RE.search(head[max(0, match.start() - 60):match.start()]):
                cited_end = match.end()
                continue
            # "Substitute Bill No. 1 for Bill No. 133, 2026" and "Veto message
            # Bill No. 2 for Bill No. 182, 2025" are about the bill after "for".
            if _DEFERS_TO_RE.match(head[match.end():match.end() + 40]):
                continue
            # An aside that names something before the instrument cites it:
            # "Attendance Reports (Portage County Ordinance 3.1.47)". A bare
            # "(Resolution 2026-35)" closing a title is the item's own.
            opened = head.rfind("(", 0, match.start())
            if opened > head.rfind(")", 0, match.start()) and head[opened + 1:match.start()].strip():
                continue
            printed = _PRINTED_YEAR_RE.match(head[match.end():match.end() + 16])
            year = printed.group(1) if printed and _BARE_NUMBER_RE.fullmatch(token) else None
            return Identifier(f"{label} {token.upper()}", matter_type, year)

    leading = extract_leading_file_token(title)
    if leading:
        return Identifier(leading, None)
    for pattern in (_PAREN_CODE_RE, _DASH_CODE_RE):
        match = pattern.search(title or "")
        if match:
            return Identifier(match.group(1), None)

    return None
