"""Name hygiene shared by attendance and vote-list parsing."""

import re
import unicodedata
from typing import List

# Honorifics and offices that precede a name in minutes. Kept generous on
# purpose: a stripped title never harms resolution, a retained one breaks it.
_TITLE_RE = re.compile(
    r"^\s*(?:"
    r"vice[-\s]?chair(?:person|woman|man)?|chair(?:person|woman|man)?|"
    r"ald(?:\.|erman|erwoman|erperson|erpersons)?|council\s*(?:members?|man|woman|ors?|persons?)|"
    r"councilmembers?|councilors?|commissioners?|supervisors?|trustees?|directors?|"
    r"(?:board|committee|commission|council|planning)\s+members?|members?|"
    r"deputy\s+mayor|mayor\s+pro\s*[- ]?tem|mayor|acting\s+chair(?:person)?|"
    r"president\s+pro\s*[- ]?tem|president|selectman|selectwoman|selectperson|"
    r"representative|senator|judge|dr|mr|mrs|ms|miss|hon)\.?\s+",
    re.IGNORECASE,
)
_SUFFIX_RE = re.compile(r"\s*,?\s*(?:jr\.?|sr\.?|ii|iii|iv)\s*$", re.IGNORECASE)
# A roster line often runs "…Aaron Van Krey Members Absent 0"; the role word
# rides along on the last name.
_TRAILING_ROLE_RE = re.compile(
    r"\s+(?:members?|commissioners?|aldermen|alderpersons?|councilmembers?|trustees?|present|absent|excused)\s*$",
    re.IGNORECASE,
)
_PARENS_RE = re.compile(r"\([^)]*\)")
_LEAD_JUNK_RE = re.compile(r"^[^A-Za-z]+")
# A fragment that starts with one of these is sentence debris swept up by a
# name-list split ("for the vote", "and the motion"), never a person.
_STOPWORD_START_RE = re.compile(
    r"^(?:for|the|and|with|that|this|all|any|was|were|who|has|have|not|per|via|"
    r"from|into|upon|to|of|in|on|at|by|as|is|be)\b",
    re.IGNORECASE,
)
_SPLIT_RE = re.compile(r"\s*(?:,|;|\band\b|&)\s*")


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
    # A title with nobody behind it ("Vice Chair", "Chair") is not a name.
    if _TITLE_RE.match(name + " x"):
        return ""
    return name


def split_names(blob: str) -> List[str]:
    """'Flynn, Gilmore, and Hinds' / 'Supervisor Tam; Supervisor Miley' -> names."""
    names = []
    for part in _SPLIT_RE.split(blob):
        cleaned = clean_name(part)
        if cleaned and looks_like_name(cleaned) and not re.fullmatch(r"(?:none|nil|n/a|-)", cleaned, re.I):
            names.append(cleaned)
    return names
