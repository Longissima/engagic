"""Rank candidate documents for one meeting slot.

Every adapter was written as "find the minutes document", and every adapter
that did so takes whichever candidate the DOM or the API happened to list
first. A meeting does not have one minutes document. It has two to four
representations of the same record: a PDF, an HTML viewer wrapping that PDF,
sometimes a DOCX, sometimes a draft published before the approved copy, and
sometimes a closed-session document sitting beside the open one.

Picking by position gets this wrong in every direction. PrimeGov stored a
one-kilobyte HTML stub over a real PDF 372 times. Destiny stored a viewer
URL for 250 rows that each had a direct PDF. Ross stored the closed-session
minutes over the regular ones because the closed link came first in the DOM.

So the shared operation is ranking, not finding. Adapters supply whatever
they know about each candidate -- a URL always, a label and a vendor format
code when the portal gives them -- and get back the one an archivist would
choose. Ordering, strongest signal first:

  1. session:  open beats closed/executive, because they are different records
  2. status:   approved/adopted/final beats draft/unapproved
  3. payload:  a document beats a page that merely displays one
  4. format:   PDF beats DOCX beats HTML

Only rank 1 changes which record is stored; 2 through 4 change which copy of
the same record is stored. Nothing here decides whether a candidate is
minutes at all -- that stays with the adapter, which is the only layer that
knows its portal's vocabulary.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence
import re

# A page that renders a document rather than being one. Matching here costs a
# rank, never a rejection: for some cities the viewer is all there is.
_VIEWER_PATTERNS = (
    r"minutesviewer\.php",
    r"documentviewer\.php",
    r"/public/minutes/",
    r"[?&]dsp=min(?![a-z])",
    r"meetingview\.aspx",
    r"docs\.google\.com/(?:gview|viewer)",
    r"[?&]doctype=",
    r"fileview\.aspx",
    r"/viewfile/",
)
_VIEWER_RE = re.compile("|".join(_VIEWER_PATTERNS), re.IGNORECASE)

_DRAFT_RE = re.compile(r"\b(?:draft|unapproved|unofficial|preliminary|proposed)\b", re.IGNORECASE)
_APPROVED_RE = re.compile(r"\b(?:approved|adopted|final|signed|official)\b", re.IGNORECASE)
_CLOSED_RE = re.compile(
    r"\b(?:closed|executive)[\s_-]*session\b|\bconfidential\b|_closed_|closed[\s_-]*minutes",
    re.IGNORECASE,
)

# Extension or vendor format word to a rank. Lower is better.
_FORMAT_RANK = {"pdf": 0, "docx": 1, "doc": 1, "rtf": 2, "html": 3, "htm": 3, "txt": 4}
_EXTENSION_RE = re.compile(r"\.([A-Za-z0-9]{2,4})(?:$|[?#])")


@dataclass(frozen=True)
class DocumentCandidate:
    """One representation of a meeting document.

    ``url`` is required. ``label`` is any human text the portal showed beside
    it (link text, title attribute, API type or template name) and is what
    draft/approved/closed detection reads. ``document_format`` is a vendor's
    own declaration ("pdf", "html", a MIME type, or a file name) for the
    common case where the URL carries no extension.
    """

    url: str
    label: str = ""
    document_format: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict, compare=False)


def _format_rank(candidate: DocumentCandidate) -> int:
    for source in (candidate.document_format, candidate.url):
        if not source:
            continue
        token = source.strip().lower()
        if token in _FORMAT_RANK:
            return _FORMAT_RANK[token]
        if "/" in token and token.rsplit("/", 1)[-1] in _FORMAT_RANK:
            return _FORMAT_RANK[token.rsplit("/", 1)[-1]]  # "application/pdf"
        match = _EXTENSION_RE.search(token)
        if match and match.group(1).lower() in _FORMAT_RANK:
            return _FORMAT_RANK[match.group(1).lower()]
    return 5


def _is_closed(candidate: DocumentCandidate) -> bool:
    return bool(_CLOSED_RE.search(candidate.label) or _CLOSED_RE.search(candidate.url))


def _is_draft(candidate: DocumentCandidate) -> bool:
    if _APPROVED_RE.search(candidate.label):
        return False
    return bool(_DRAFT_RE.search(candidate.label) or _DRAFT_RE.search(candidate.url))


def _is_viewer(candidate: DocumentCandidate) -> bool:
    return bool(_VIEWER_RE.search(candidate.url))


def rank_key(candidate: DocumentCandidate, prefer_closed: bool = False) -> tuple:
    """Sort key for a candidate. Lower sorts better."""
    closed = _is_closed(candidate)
    return (
        0 if closed == prefer_closed else 1,
        1 if _is_draft(candidate) else 0,
        1 if _is_viewer(candidate) else 0,
        _format_rank(candidate),
    )


def pick_document(
    candidates: Iterable[DocumentCandidate],
    prefer_closed: bool = False,
) -> Optional[DocumentCandidate]:
    """Best representation among candidates, or None when there are none.

    Ties keep the portal's own order, so a vendor that already lists its
    preferred copy first is never reordered arbitrarily.
    """
    ordered = [c for c in candidates if c and c.url]
    if not ordered:
        return None
    return min(ordered, key=lambda c: rank_key(c, prefer_closed))


def pick_document_url(
    candidates: Iterable[DocumentCandidate],
    prefer_closed: bool = False,
) -> Optional[str]:
    """pick_document, returning just the URL."""
    best = pick_document(candidates, prefer_closed)
    return best.url if best else None


# Words that mark a link as the minutes record. Kept narrow: "minutes" is the
# near-universal term, and the handful of portals that use something else say
# so in the same breath ("Journal of Proceedings", "Legal Minutes").
_MINUTES_WORD_RE = re.compile(r"\bminutes?\b|\bjournal\b|\bproceedings\b", re.IGNORECASE)
# Things that merely mention minutes without being them.
_NOT_MINUTES_RE = re.compile(
    r"\bagenda\b(?!.*\bminutes\b)|approval\s+of\s+(?:the\s+)?minutes|minutes?\s+of\s+the\s+\w+\s+meeting\s+(?:will|are)\b"
    r"|\bminute\s+(?:order|taker|clerk)\b|\b\d+\s*minutes?\b",
    re.IGNORECASE,
)


_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _words(text: str) -> str:
    """Split identifiers into words so element ids read like labels.

    Some portals carry the only occurrence of the word in an element id
    ("ctl00_hypMinutesPDF"), where it has no word boundary of its own.
    """
    return _CAMEL_RE.sub(" ", text).replace("_", " ").replace("-", " ")


def looks_like_minutes(text: str) -> bool:
    """True when a link's own words say it is the minutes record."""
    if not text:
        return False
    spaced = _words(text)
    if _NOT_MINUTES_RE.search(text) or _NOT_MINUTES_RE.search(spaced):
        return False
    return bool(_MINUTES_WORD_RE.search(spaced))


def find_minutes_links(soup_or_tag, base_url: str = "") -> List[DocumentCandidate]:
    """Every anchor in a page or row whose own words say "minutes".

    The vendor-neutral fallback for adapters whose portal exposes no field or
    selector: search the markup for links that call themselves minutes and
    rank what comes back. Every attribute a portal might carry the word in is
    read, because which one it uses is exactly what varies between cities --
    some put it in the link text, some only in a title or an alt on an icon,
    some only in the element id.

    Deliberately a fallback, not a replacement: a portal that names its
    documents in an API field is more reliable than its own markup, and that
    path should be tried first.
    """
    from urllib.parse import urljoin

    candidates: List[DocumentCandidate] = []
    seen = set()
    for anchor in soup_or_tag.find_all("a", href=True):
        href = anchor.get("href") or ""
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        words = [
            anchor.get_text(" ", strip=True),
            str(anchor.get("title") or ""),
            str(anchor.get("aria-label") or ""),
            str(anchor.get("id") or ""),
        ]
        # An icon link carries its words on the image, not the anchor.
        for image in anchor.find_all("img"):
            words.append(str(image.get("alt") or ""))
            words.append(str(image.get("title") or ""))
        label = " ".join(w for w in words if w).strip()
        if not looks_like_minutes(label) and not looks_like_minutes(href):
            continue
        url = urljoin(base_url, href) if base_url else href
        if url in seen:
            continue
        seen.add(url)
        candidates.append(DocumentCandidate(url=url, label=label))
    return candidates


def candidates_from_pairs(pairs: Sequence[tuple]) -> List[DocumentCandidate]:
    """Build candidates from (url, label) or (url, label, format) tuples."""
    built: List[DocumentCandidate] = []
    for pair in pairs:
        if not pair or not pair[0]:
            continue
        url = pair[0]
        label = pair[1] if len(pair) > 1 and pair[1] else ""
        document_format = pair[2] if len(pair) > 2 else None
        built.append(DocumentCandidate(url=url, label=label, document_format=document_format))
    return built
