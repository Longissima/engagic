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
from parsing.pdf import PdfExtractor
from vendors.adapters.parsers.agenda_chunker import parse_agenda_pdf
from vendors.adapters.parsers.agenda_chunker_v2 import parse_agenda_pdf_v2
from vendors.adapters.parsers.morphology import classify
from vendors.adapters.parsers.pdf_profile import PdfProfile, profile_doc
from vendors.adapters.parsers.quality import (
    extract_matter_files,
    garbage_titles,
    repair_titles,
    segmentation_smell,
)
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
TIMEOUT = "timeout"                # guard killed a wedged/runaway chunk (set by dispatch)
DEFERRED = "deferred_to_processing"  # sync archived the bytes; the processor manufactures shape
OCR_REQUIRED = "ocr_required"       # text-derived item boundaries intersect incomplete OCR pages

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

# These methods derive item boundaries from document-authored structure,
# rather than from the embedded text that may itself require OCR.  The body
# text can be partial while the boundary remains authoritative.  text_items
# is intentionally absent: when its heading page needs OCR, the shape itself
# is upstream-broken and must be rebuilt in the OCR-owning process lane.
_STRUCTURAL_PARSE_METHODS = frozenset({
    "url",
    "v2_url",
    "v2_pageref",
    "v2_toc",
    "toc_hierarchical",
    "toc_document_bundle",
    "toc_deep_hierarchical",
    "toc_flat",
})

_INCOMPLETE_TEXT_MARKER = "[SOURCE EXTRACTION INCOMPLETE:"

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
        "quality": winner.get("quality") if winner else None,
        "morphology": winner.get("morphology") if winner else None,
        "profile": winner.get("profile") if winner else None,
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
    hint_source: str = "ladder_default"
    hint_rung: Optional[str] = None
    declared_first_rung: Optional[str] = None
    quality: Dict[str, Any] = field(default_factory=dict)  # extraction vs chunking layer signals
    # Ground-truth text manufactured alongside chunking (stage 2 of
    # docs/CORPUS_ARCHITECTURE.md): the full PdfExtractor result dict
    # (text/method/page_count/ocr_pages/ocr_pending), produced with OCR
    # disabled while the child already holds the document. ocr_pending == 0
    # means the text is exactly what the OCR-enabled process extractor would
    # produce, so the base adapter persists it to the corpus. Never part of
    # audit() -- the text can be megabytes and audits land in queue metadata.
    extraction: Optional[Dict[str, Any]] = None

    @property
    def parse_method(self) -> str:
        return self.metadata.get("parse_method", "")

    def audit(self) -> Dict[str, Any]:
        """Compact JSON-safe trail for logs / queue.processing_metadata."""
        return {
            "audit_version": "ca2",
            "ladder": self.ladder,
            "profile": self.profile.to_dict() if self.profile else None,
            "morphology": self.morphology,
            "suggested_rung": self.suggested_rung,
            "suggestion_used": self.suggestion_used,
            "hint_source": self.hint_source,
            "hint_rung": self.hint_rung,
            "declared_first_rung": self.declared_first_rung,
            # the passive confusion matrix: did the classifier call it?
            "suggestion_agreed": (
                self.suggested_rung == self.winning_rung
                if self.suggested_rung and self.winning_rung else None
            ),
            "quality": self.quality or None,
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


def _attach_ground_truth(result: ChunkResult, pdf_path: str) -> None:
    """Manufacture full raw text while this child already holds the document.

    The stage-2 write-once law: sync is one of the two places bytes get
    parsed, so producing the corpus text here means process never re-extracts
    what sync already read. OCR stays disabled -- pure scans and mixed docs
    belong to the OCR-owning process path -- and the profile gates the pass so
    obviously scanned documents don't pay a full-document read for nothing.
    A failure here costs only the text; the chunk items already stand.
    """
    if result.profile is None or not result.profile.has_text_layer:
        return
    try:
        t0 = time.monotonic()
        # Defaults match the process extractor (ocr_threshold=100,
        # detect_legislative_formatting=True): with ocr_pending == 0 the
        # output is identical to what process extraction would produce,
        # which is what makes persisting it sound.
        extractor = PdfExtractor(ocr_enabled=False)
        result.extraction = extractor.extract_from_path(pdf_path)
        result.extraction["extraction_time"] = round(time.monotonic() - t0, 3)
    except Exception as e:
        logger.debug("ground-truth text pass failed", error=str(e), error_type=type(e).__name__)


def _page_range(item: Dict[str, Any]) -> Optional[range]:
    """Return an item's inclusive 1-indexed source-page range, if known."""
    metadata = item.get("metadata") or {}
    raw_start = metadata.get("page_start")
    if raw_start is None:
        return None
    try:
        start = int(str(raw_start))
        end = int(str(metadata.get("page_end") or start))
    except (TypeError, ValueError):
        return None
    if start < 1 or end < start:
        return None
    return range(start, end + 1)


def _format_pages(pages: Sequence[int]) -> str:
    if not pages:
        return "unknown"
    runs: List[str] = []
    start = previous = pages[0]
    for page in pages[1:]:
        if page == previous + 1:
            previous = page
            continue
        runs.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = page
    runs.append(str(start) if start == previous else f"{start}-{previous}")
    return ", ".join(runs)


def _apply_ocr_shape_policy(result: ChunkResult) -> None:
    """Separate authoritative shape from incomplete page text.

    OCR-disabled ground truth is allowed to carry structurally delineated
    items, but never to bless text-derived boundaries that intersect a page
    still requiring OCR.  Retained items get an explicit model-visible note
    and per-item page provenance; produce_ground_truth later turns that
    provenance into a source-page link once it has the source URL.
    """
    if not result.items:
        return

    extraction = result.extraction or {}
    pending_count = int(extraction.get("ocr_pending") or 0)
    pending_values = extraction.get("ocr_pending_pages")
    pending_pages: Optional[set[int]]
    if pending_values is not None:
        pending_pages = {
            int(str(page)) for page in pending_values
            if isinstance(page, int) or str(page).isdigit()
        }
    else:
        pending_pages = None

    # A fully scanned document deliberately skips the full text pass.  TOC
    # and link shapes can still be valid, but every item is conservatively
    # marked incomplete because exact OCR pages are not known here.
    if not result.extraction and result.profile and not result.profile.has_text_layer:
        pending_count = result.profile.page_count
        pending_pages = None

    if pending_count <= 0:
        return

    affected: List[tuple[Dict[str, Any], List[int]]] = []
    for item in result.items:
        item_range = _page_range(item)
        if pending_pages is None:
            pages = list(item_range) if item_range is not None else []
            affected.append((item, pages))
            continue
        if item_range is None:
            affected.append((item, sorted(pending_pages)))
            continue
        pages = sorted(pending_pages.intersection(item_range))
        if pages:
            affected.append((item, pages))

    if not affected:
        return

    if result.parse_method not in _STRUCTURAL_PARSE_METHODS:
        result.items = []
        result.raw = {}
        result.failure_reason = OCR_REQUIRED
        winning_rung = result.winning_rung
        result.winning_rung = None
        for attempt in reversed(result.attempts):
            if attempt.rung == winning_rung and attempt.failure_reason is None:
                attempt.item_count = 0
                attempt.failure_reason = OCR_REQUIRED
                attempt.error = (
                    f"{len(affected)} text-derived item boundaries intersect "
                    "pages requiring OCR"
                )
                break
        return

    for item, pages in affected:
        metadata = dict(item.get("metadata") or {})
        metadata.update({
            "extraction_incomplete": True,
            "ocr_pending_pages": pages,
            "shape_basis": "structural",
        })
        item["metadata"] = metadata
        note = (
            f"{_INCOMPLETE_TEXT_MARKER} OCR is still required on source "
            f"page(s) {_format_pages(pages)}. The item boundary comes from "
            "the document's links/bookmarks; verify the retained text "
            "against the linked source page.]"
        )
        body_text = str(item.get("body_text") or "").strip()
        if not body_text.startswith(_INCOMPLETE_TEXT_MARKER):
            item["body_text"] = f"{note}\n\n{body_text}".rstrip()


def recover_pdf_text(pdf_path: str, ladder: str = "auto") -> ChunkResult:
    """Reduced text-only recovery after a native cascade child crash.

    This deliberately avoids PDF profiling, link traversal, TOC parsing, and
    both full agenda engines.  It can therefore salvage complete corpus text
    (and simple numbered items) when a malformed structure crashes the normal
    cascade.  It remains subprocess-guarded by the caller.
    """
    result = ChunkResult(ladder=ladder)
    try:
        result.extraction = PdfExtractor(ocr_enabled=False).extract_from_path(pdf_path)
    except Exception as e:
        result.failure_reason = ENGINE_ERROR
        result.attempts.append(Attempt(
            rung="recovery:text",
            failure_reason=ENGINE_ERROR,
            error=f"{type(e).__name__}: {e}",
        ))
        return result

    t0 = time.monotonic()
    try:
        parsed = parse_agenda_pdf_text(pdf_path)
    except Exception as e:
        result.failure_reason = ENGINE_ERROR
        result.attempts.append(Attempt(
            rung="recovery:text",
            failure_reason=ENGINE_ERROR,
            error=f"{type(e).__name__}: {e}",
            duration_ms=int((time.monotonic() - t0) * 1000),
        ))
        return result

    items = parsed.get("items") or []
    parse_method = (parsed.get("metadata") or {}).get("parse_method", "")
    result.attempts.append(Attempt(
        rung="text:auto",
        item_count=len(items),
        parse_method=parse_method,
        failure_reason=None if items else NO_ITEMS,
        duration_ms=int((time.monotonic() - t0) * 1000),
    ))
    if items:
        result.items = items
        result.metadata = parsed.get("metadata") or {}
        result.raw = parsed
        result.winning_rung = "text:auto"
        result.quality = {
            "recovery": "text_only_after_guard_crash",
            "garbage_titles": len(garbage_titles(items)),
            "repaired_titles": 0,
            "matter_files": extract_matter_files(items),
        }
        _apply_ocr_shape_policy(result)
    else:
        text = str(result.extraction.get("text") or "").strip()
        result.failure_reason = NO_ITEMS if text else NO_TEXT_LAYER
    return result


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
    declared_rungs = (
        LADDERS[ladder] if isinstance(ladder, str) else list(ladder)
    )
    result.declared_first_rung = declared_rungs[0] if declared_rungs else None
    if hint is not None:
        result.hint_source = "sticky"
        result.hint_rung = hint

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
            result.hint_source = "classifier"
            result.hint_rung = hint

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

            # Quality signals, by failure layer: garbage titles = extraction
            # problem (repairable from the item's own page); count diverging
            # from the document's own numbering = chunking problem. Matter
            # files harvest before repair, which strips the same prefix.
            matter_files = extract_matter_files(items)
            repaired = repair_titles(items, pdf_path)
            result.quality = {
                "garbage_titles": len(garbage_titles(items)),  # post-repair
                "repaired_titles": repaired,
                "matter_files": matter_files,
                "seg_smell": segmentation_smell(
                    result.profile.item_number_lines if result.profile else 0,
                    len(items),
                ),
            }
            _attach_ground_truth(result, pdf_path)
            _apply_ocr_shape_policy(result)
            return result

    result.failure_reason = _classify_empty(pdf_path, result.attempts, result.profile)
    # No items is not no text: a flat agenda that defeated every rung still
    # carries corpus-worthy ground truth.
    _attach_ground_truth(result, pdf_path)
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
