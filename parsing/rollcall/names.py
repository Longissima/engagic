"""Name hygiene shared by attendance and vote-list parsing."""

import re
import unicodedata
from typing import List

# Honorifics and offices that precede a name in minutes. Kept generous on
# purpose: a stripped title never harms resolution, a retained one breaks it.
_TITLE_RE = re.compile(
    r"^\s*(?:"
    # A seat label precedes the name, with or without the office word. Waco's
    # "District 1 Isabel Lozano" otherwise keeps the digit and is rejected as
    # not-a-name; Atlanta stores "Post 1 At Large Michael Julian Bond", whose
    # first token keys the first-initial variant as "p. bond", so the minutes'
    # "M. Bond" could never match it.
    r"(?:district|post|ward|seat|zone|division|precinct|place|position|group)"
    r"\s+(?:[IVX]+|\d+)(?:\s+at[-\s]large)?(?:\s+supervisor)?|"
    r"at[-\s]large(?:\s+(?:[IVX]+|\d+))?|"
    r"ex[-\s]?officio(?:\s+(?:county|city|town|village|board|council|committee))*|"
    r"vice[-\s]?chair(?:person|woman|man)?|chair(?:person|woman|man)?|"
    r"ald(?:\.|er|erman|erwoman|erperson|erpersons|ermen)?|council\s*(?:members?|man|woman|ors?|persons?)|"
    r"councilmembers?|councilors?|commissioners?|supervisors?|trustees?|directors?|"
    r"(?:board|committee|commission|council|planning|agency|authority|district)\s+members?|members?|"
    r"(?:board|committee|commission|council|city|county|town|village)\s+(?:vice\s+)?president|"
    r"vice\s+mayor|vice\s+president|deputy\s+mayor|mayor\s+pro\s*[- ]?tem(?:pore)?|mayor|acting\s+chair(?:person)?|"
    r"president\s+pro\s*[- ]?tem(?:pore)?|president|selectman|selectwoman|selectperson|"
    # An abbreviated title's period is its own separator: Milwaukee prints
    # "Ald.Westmoreland" with no space, and requiring one left the title glued
    # to the name so it resolved to nobody. Whitespace is still required when
    # no period is present, which keeps "Alderson" and "Drake" from matching.
    r"representative|senator|judge|dr|mr|mrs|ms|miss|hon)(?:\.\s*|\s+)",
    re.IGNORECASE,
)
# A generational suffix is not a surname. Left unstripped, "Chambers Jr" keys
# its surname variant as "jr", so the roster's "Michael Chambers Jr" and the
# minutes' "Chambers Jr" never meet. Stripping runs on both sides of the match
# because Gazetteer cleans roster names through this same function.
_SUFFIX_RE = re.compile(r"\s*,?\s*(?:jr\.?|sr\.?|ii|iii|iv)\s*$", re.IGNORECASE)
# An office word with nobody attached is not a person. "Council Member, District 1"
# splits on the comma, the trailing role strips "Member", and a member named
# "Council" is created -- 14 such rows exist.
_BARE_OFFICE_RE = re.compile(
    r"^(?:council|village|town|city|county|district|board|committee|commission|planning|"
    r"mayor|chair|member|trustee|alder(?:man|woman|person)?|ald|supervisor|commissioner|"
    r"pro\s*[- ]?tem(?:pore)?|ex[-\s]?officio|at[-\s]large|"
    r"director|president|staff|present|absent|excused|attendance|attendee)$",
    re.IGNORECASE,
)
# A roster line often runs "…Aaron Van Krey Members Absent 0"; the role word
# rides along on the last name.
_TRAILING_ROLE_RE = re.compile(
    r"\s+(?:members?|commissioners?|aldermen|alderpersons?|councilmembers?|trustees?|present|absent|excused)\s*$",
    re.IGNORECASE,
)
_PARENS_RE = re.compile(r"\([^)]*\)")
# "Mayor Pro Tem/Councilor District 4 Mike Battaglino" joins two offices on one
# seat. Only a slash that FOLLOWS a title word collapses: "Nelson/Lant" is a
# mover and a seconder, and merging those two alders invents a person.
# "Council / Agency Member Alcantar Loza" and "Mayor Pro Tem/Councilor District 4
# Mike Battaglino" name one seat holding two offices. Joining the halves only
# builds a title chain the stripper cannot peel, so the title-only half is
# dropped instead -- and only when every word in it is a title, which is what
# keeps "Nelson/Lant" (a mover and a seconder) two people.
_OFFICE_WORD_RE = re.compile(
    r"^(?:vice|deputy|acting|interim|pro|tem|tempore|at|large|and|the|"
    r"mayor|chair|chairperson|chairwoman|chairman|president|councilor|councilmember|"
    r"council|member|members|alderman|alderwoman|alderperson|alder|commissioner|"
    r"commission|supervisor|trustee|director|agency|authority|board|committee|"
    r"district|city|town|village|county|successor|housing|joint|clerk|treasurer)$",
    re.IGNORECASE,
)


def _drop_title_alias(name: str) -> str:
    """Drop a slash-separated alias for the same seat, title-only side first."""
    while "/" in name:
        left, right = name.split("/", 1)
        words = [w for w in re.split(r"[\s.]+", left) if w]
        if not words or not all(_OFFICE_WORD_RE.match(w) for w in words):
            return name
        name = right.strip()
    return name
_LEAD_JUNK_RE = re.compile(r"^[^A-Za-z]+")
# A fragment that starts with one of these is sentence debris swept up by a
# name-list split ("for the vote", "and the motion"), never a person.
_STOPWORD_START_RE = re.compile(
    r"^(?:for|the|and|with|that|this|all|any|was|were|who|has|have|not|per|via|"
    r"from|into|upon|to|of|in|on|at|by|as|is|be)\b",
    re.IGNORECASE,
)
_SPLIT_RE = re.compile(r"\s*(?:,|;|\band\b|&)\s*")
# Milwaukee prints a two-voter line as "Ald.Dimitrijevic Ald.Stamper" with no
# comma, which read as one unresolvable name. Only the period-abbreviated form
# starts a new name here: a spelled-out title would split "Village Trustee Eder"
# into a person named "Village".
_REPEATED_ABBREV_TITLE_RE = re.compile(
    r"(?<=[A-Za-z])\s+(?=(?:Ald|Dr|Mr|Mrs|Ms|Hon)\.)", re.IGNORECASE
)


def fold(text: str) -> str:
    """Accent-insensitive lowercase key: 'Joaquín Baca' and 'Joaquin Baca' are one person."""
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    ).lower()


def looks_like_name(text: str) -> bool:
    words = text.split()
    if not (1 <= len(words) <= 4) or re.search(r"[\d:;/]", text):
        return False
    if _STOPWORD_START_RE.match(text):
        return False
    # Every real name carries at least one capitalized word.
    return any(w[:1].isupper() for w in words)


_TRAILING_CLAUSE_RE = re.compile(r"\s+(?:The|Motion|Second|Seconded|Vote|It)\b.*$")
_SENTENCE_BREAK_RE = re.compile(r"\.\s+(?=\S)")


def _cut_sentence_tail(raw: str) -> str:
    """Drop the sentence that follows a name, keeping honorifics intact.

    "Ron Leino. Motion passed" is a name and then prose. "Dr. Michael Aiello"
    is one name: the period belongs to the honorific, which is why the break
    only counts after a word long enough not to be a title or an initial.
    """
    text = _TRAILING_CLAUSE_RE.sub("", raw)
    for match in _SENTENCE_BREAK_RE.finditer(text):
        preceding = text[:match.start()].split()
        if preceding and len(preceding[-1]) > 3:
            return text[:match.start()]
    return text


def clean_name(raw: str) -> str:
    """Strip titles, parentheticals and trailing punctuation; collapse whitespace.

    A name captured out of running prose keeps the sentence behind it
    ("Ron Leino. Motion passed"); cut at the sentence break so the roster
    lookup sees the name and nothing else.
    """
    name = _cut_sentence_tail(raw)
    name = _PARENS_RE.sub("", name)
    name = _drop_title_alias(name)
    name = _LEAD_JUNK_RE.sub("", name)
    name = re.sub(r"\s+", " ", name).strip(" ,.;:-–")
    previous = None
    while previous != name:
        previous = name
        name = _TITLE_RE.sub("", name).strip(" ,.")
    previous = None
    while previous != name:
        previous = name
        name = _TRAILING_ROLE_RE.sub("", name).strip(" ,.")
    name = _SUFFIX_RE.sub("", name).strip(" ,.")
    # A title with nobody behind it ("Vice Chair", "Chair") is not a name.
    if _TITLE_RE.match(name + " x") or _BARE_OFFICE_RE.match(name):
        return ""
    return name


def split_names(blob: str) -> List[str]:
    """'Flynn, Gilmore, and Hinds' / 'Supervisor Tam; Supervisor Miley' -> names."""
    names = []
    for chunk in _SPLIT_RE.split(blob):
        for part in _REPEATED_ABBREV_TITLE_RE.split(chunk):
            cleaned = clean_name(part)
            if cleaned and looks_like_name(cleaned) and not re.fullmatch(r"(?:none|nil|n/a|-)", cleaned, re.I):
                names.append(cleaned)
    return names
