"""Declarative cascade router over the agenda PDF chunkers.

A *rung* is one engine invocation, written "engine:method" — e.g. "v2:toc"
runs agenda_chunker_v2 with force_method="toc". A *ladder* is an ordered
list of rungs tried until one yields items. Ladders replace the old
force_method if/elif dispatch in base_adapter_async: routing policy is
data, every attempt is recorded, and total failures get a classified
reason instead of a silent empty list.

chunk_pdf() returns a ChunkResult carrying the winning engine output plus
the full attempt audit — tests assert on it, and it serializes (audit())
for queue.processing_metadata.

Engines:
  v1 = agenda_chunker.parse_agenda_pdf      (methods: toc, url, auto)
  v2 = agenda_chunker_v2.parse_agenda_pdf_v2 (methods: toc, url, pageref, auto)
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Union

import fitz

from config import config, get_logger
from vendors.adapters.parsers.agenda_chunker import parse_agenda_pdf
from vendors.adapters.parsers.agenda_chunker_v2 import parse_agenda_pdf_v2
from vendors.adapters.parsers.morphology import classify
from vendors.adapters.parsers.pdf_profile import PdfProfile, profile_doc
from vendors.adapters.parsers.text_chunker import parse_agenda_pdf_text

logger = get_logger(__name__)

# Failure reasons (ChunkResult.failure_reason / Attempt.failure_reason)
DOWNLOAD_FAILED = "download_failed"  # URL fetch failed (set by base adapter)
TOO_SMALL = "too_small"            # under MIN_PDF_BYTES, not a real PDF
OPEN_FAILED = "open_failed"        # fitz cannot open the file
ENCRYPTED = "encrypted"            # password-protected
NO_TEXT_LAYER = "no_text_layer"    # scanned/image-only, nothing to anchor on
NO_ITEMS = "no_items"              # parsed fine, no item structure found
ENGINE_ERROR = "engine_error"      # chunker raised

MIN_PDF_BYTES = 500

_ENGINE_FUNCS = {
    "v1": parse_agenda_pdf,
    "v2": parse_agenda_pdf_v2,
    "text": parse_agenda_pdf_text,
}

_VALID_METHODS = {
    "v1": {"toc", "url", "auto"},
    "v2": {"toc", "url", "pageref", "auto"},
    "text": {"auto"},
}

# Every ladder ends on text:auto — the flat-text extractor for short
# agendas whose only structure is numbered heading lines (no links, no
# usable outline). It self-limits (<=20 pages, 3-80 headings), so as a
# terminal rung it only converts former no_items failures.
LADDERS: Dict[str, List[str]] = {
    # agenda_url chain: short hyperlinked agendas. v2 url-anchor first;
    # v1 url catches URL-anchored layouts v2 misaligns (Ontario CA);
    # v2 auto sweeps up layouts v1's `\d{1,2}\.` item regex can't
    # match (Winter Springs FL uses 3-digit item numbers).
    "agenda": ["v2:url", "v1:url", "v2:auto", "text:auto"],
    # packet_url chain: compiled packets with bookmark trees.
    "packet": ["v2:toc", "text:auto"],
    # legacy force_method="url" semantics: v1 first, v2 auto fallback.
    "url_legacy": ["v1:url", "v2:auto", "text:auto"],
    # unforced: v2 auto-detect (toc/url/pageref/url_then_toc), v1 fallback.
    "auto": ["v2:auto", "v1:auto", "text:auto"],
    # legacy force_method="v2_url": single rung, no fallback.
    "v2_url_only": ["v2:url"],
}

# Old force_method strings -> ladder names, for the base-adapter shim.
_FORCE_METHOD_TO_LADDER = {
    None: "auto",
    "toc": "packet",
    "url": "url_legacy",
    "v2_url": "v2_url_only",
}


def ladder_for_force_method(force_method: Optional[str]) -> str:
    """Map a legacy force_method string to its equivalent ladder name."""
    try:
        return _FORCE_METHOD_TO_LADDER[force_method]
    except KeyError:
        raise ValueError(f"unknown force_method: {force_method!r}")


def resolve_rungs(
    ladder: Union[str, Sequence[str]], hint: Optional[str] = None
) -> List[str]:
    """Expand a ladder to its rungs, promoting the hint rung to the front.

    The hint is a city's last winning rung — cities regenerate the same
    layout every meeting, so trying it first collapses the steady-state
    cascade to one attempt. A hint not in the ladder is ignored: hints
    reorder, they never add rungs.
    """
    rungs = list(LADDERS[ladder]) if isinstance(ladder, str) else list(ladder)
    if hint and hint in rungs and rungs[0] != hint:
        rungs.remove(hint)
        rungs.insert(0, hint)
    return rungs


# ---------------------------------------------------------------------------
# Sticky per-city hints: (vendor, slug, ladder) -> last winning rung.
# Process-local; the fetcher seeds it at startup from the audit trail
# persisted in queue.processing_metadata, so it survives restarts without
# any schema addition. Dict ops are GIL-atomic — no locking needed for
# str->str updates from chunker worker threads.
# ---------------------------------------------------------------------------

_city_hints: Dict[tuple, str] = {}


def get_city_hint(vendor: str, slug: str, ladder: str) -> Optional[str]:
    return _city_hints.get((vendor, slug, ladder))


def set_city_hint(vendor: str, slug: str, ladder: str, rung: str) -> None:
    if vendor and slug and ladder and rung:
        _city_hints[(vendor, slug, ladder)] = rung


def seed_city_hints(rows: Sequence[Dict[str, Any]]) -> int:
    """Bulk-load hints (rows of vendor/slug/ladder/rung dicts) from the DB."""
    for r in rows:
        set_city_hint(r["vendor"], r["slug"], r["ladder"], r["rung"])
    return len(rows)


def summarize_runs(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Collapse one meeting's cascade runs into a queue-storable summary.

    A meeting may chunk more than one PDF (agenda first, packet fallback);
    the summary surfaces the run that produced items so SQL can read
    winning_rung/winning_ladder without unpacking the runs list.
    """
    winner = next((r for r in reversed(runs) if r.get("winning_rung")), None)
    return {
        "winning_rung": winner["winning_rung"] if winner else None,
        "winning_ladder": winner.get("ladder") if winner else None,
        "parse_method": winner.get("parse_method", "") if winner else "",
        "item_count": winner.get("item_count", 0) if winner else 0,
        "failure_reason": None if winner else runs[-1].get("failure_reason"),
        "runs": runs,
    }


@dataclass
class Attempt:
    rung: str
    item_count: int = 0
    parse_method: str = ""
    failure_reason: Optional[str] = None
    error: Optional[str] = None
    duration_ms: int = 0


@dataclass
class ChunkResult:
    items: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)  # full winning engine output
    winning_rung: Optional[str] = None
    attempts: List[Attempt] = field(default_factory=list)
    failure_reason: Optional[str] = None  # set iff no rung won
    ladder: Optional[str] = None  # ladder name when invoked with a named ladder
    profile: Optional[PdfProfile] = None  # measured morphology signals
    morphology: Optional[str] = None  # classifier's named shape
    suggested_rung: Optional[str] = None  # classifier's pick (None = no opinion)
    suggestion_used: bool = False  # suggestion actually filled the hint slot

    @property
    def parse_method(self) -> str:
        return self.metadata.get("parse_method", "")

    def audit(self) -> Dict[str, Any]:
        """Compact JSON-safe trail for logs / queue.processing_metadata."""
        return {
            "ladder": self.ladder,
            "profile": self.profile.to_dict() if self.profile else None,
            "morphology": self.morphology,
            "suggested_rung": self.suggested_rung,
            "suggestion_used": self.suggestion_used,
            # the passive confusion matrix: did the classifier call it?
            "suggestion_agreed": (
                self.suggested_rung == self.winning_rung
                if self.suggested_rung and self.winning_rung else None
            ),
            "winning_rung": self.winning_rung,
            "parse_method": self.parse_method,
            "item_count": len(self.items),
            "failure_reason": self.failure_reason,
            "attempts": [
                {
                    "rung": a.rung,
                    "items": a.item_count,
                    "parse_method": a.parse_method,
                    "reason": a.failure_reason,
                    "error": a.error,
                    "ms": a.duration_ms,
                }
                for a in self.attempts
            ],
        }


def _run_rung(rung: str, pdf_path: str) -> Dict[str, Any]:
    engine, _, method = rung.partition(":")
    func = _ENGINE_FUNCS.get(engine)
    if func is None or method not in _VALID_METHODS[engine]:
        raise ValueError(f"invalid rung: {rung!r}")
    return func(pdf_path, force_method=None if method == "auto" else method)


def _classify_empty(
    pdf_path: str,
    attempts: List[Attempt],
    profile: Optional[PdfProfile] = None,
) -> str:
    """All rungs came back empty — figure out why."""
    if attempts and all(a.failure_reason == ENGINE_ERROR for a in attempts):
        return ENGINE_ERROR
    if profile is not None:
        return NO_ITEMS if profile.has_text_layer else NO_TEXT_LAYER
    try:
        doc = fitz.open(pdf_path)
        sample = min(5, doc.page_count)
        chars = sum(len(str(doc[i].get_text("text")).strip()) for i in range(sample))
        doc.close()
    except Exception:
        return OPEN_FAILED
    if chars < 50:
        return NO_TEXT_LAYER
    return NO_ITEMS


def chunk_pdf(
    pdf_path: str,
    ladder: Union[str, Sequence[str]] = "auto",
    hint: Optional[str] = None,
) -> ChunkResult:
    """Run the ladder's rungs in order until one yields items.

    Each rung failure (no items or exception) is recorded and the cascade
    continues — a crash in one engine no longer skips the fallbacks.
    A hint (the city's last winning rung) is tried first; absent one, the
    morphology classifier's suggestion fills the slot. See resolve_rungs.
    """
    result = ChunkResult(ladder=ladder if isinstance(ladder, str) else None)

    try:
        doc = fitz.open(pdf_path)
        needs_pass = doc.needs_pass
        if not needs_pass:
            # Measure morphology signals while the doc is open; the profile
            # rides the audit so prod data records what each PDF *is*.
            try:
                result.profile = profile_doc(doc)
            except Exception as e:
                logger.debug("pdf profiling failed", error=str(e))
        doc.close()
    except Exception as e:
        result.failure_reason = OPEN_FAILED
        result.attempts.append(
            Attempt(rung="preflight", failure_reason=OPEN_FAILED,
                    error=f"{type(e).__name__}: {e}")
        )
        return result
    if needs_pass:
        result.failure_reason = ENCRYPTED
        result.attempts.append(Attempt(rung="preflight", failure_reason=ENCRYPTED))
        return result

    used_suggestion = False
    if result.profile is not None:
        result.morphology, result.suggested_rung = classify(result.profile)
        if hint is None and result.suggested_rung and config.CHUNKER_CLASSIFIER_HINTS:
            hint = result.suggested_rung
            used_suggestion = True

    rungs = resolve_rungs(ladder, hint)
    # "used" means it could actually influence routing — a suggestion outside
    # this ladder's vocabulary is recorded but ignored by resolve_rungs
    if used_suggestion:
        result.suggestion_used = result.suggested_rung in rungs

    for rung in rungs:
        t0 = time.monotonic()
        try:
            parsed = _run_rung(rung, pdf_path)
        except Exception as e:
            result.attempts.append(
                Attempt(
                    rung=rung,
                    failure_reason=ENGINE_ERROR,
                    error=f"{type(e).__name__}: {e}",
                    duration_ms=int((time.monotonic() - t0) * 1000),
                )
            )
            continue

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        items = parsed.get("items") or []
        parse_method = (parsed.get("metadata") or {}).get("parse_method", "")
        attempt = Attempt(
            rung=rung,
            item_count=len(items),
            parse_method=parse_method,
            failure_reason=None if items else NO_ITEMS,
            duration_ms=elapsed_ms,
        )
        result.attempts.append(attempt)

        if items:
            result.items = items
            result.metadata = parsed.get("metadata") or {}
            result.raw = parsed
            result.winning_rung = rung
            return result

    result.failure_reason = _classify_empty(pdf_path, result.attempts, result.profile)
    return result


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("usage: python -m vendors.adapters.parsers.router <pdf> [ladder]")
        sys.exit(1)
    res = chunk_pdf(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "auto")
    print(json.dumps(res.audit(), indent=2))
    for it in res.items:
        pages = (it.get("metadata") or {}).get("page_start", "")
        print(f"  [{it.get('agenda_number', '')}] {it.get('title', '')[:70]}"
              f"  ({len(it.get('attachments') or [])} att, p{pages})")
