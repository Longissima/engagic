"""Shared document-input budgeting for streaming and batch summarization.

The model limit applies to the whole request, not just item-specific text.
These helpers keep shared context, document headings, and truncation notices
inside one deterministic character budget before prompt-template overhead.
"""

from typing import Iterable, Sequence


# Gemini Flash accepts roughly 1M input tokens. Keep about 10% of the nominal
# four-million-character approximation for the prompt, schema, and token-ratio
# variance in non-English/table-heavy documents.
MAX_ITEM_INPUT_CHARS = 3_600_000
TRIM_FLOOR_CHARS = 50_000
SHARED_CONTEXT_RESERVE_CHARS = 50_000
MAX_SHARED_CONTEXT_CHARS = (
    MAX_ITEM_INPUT_CHARS - SHARED_CONTEXT_RESERVE_CHARS - 1_000
)
PUBLIC_COMMENT_EXCERPT_CHARS = 15_000
MAX_ITEM_TITLE_CHARS = 2_000

DOCUMENT_ATTACHMENT_TYPES = frozenset(
    {
        "pdf",
        "doc",
        "document",
        "unknown",
        "spreadsheet",
        "xls",
        "presentation",
        "ppt",
    }
)

_TRUNCATION_SUFFIX = "\n\n[PIPELINE NOTE: input truncated to fit the model context window]"


def truncate_text_to_budget(text: str, budget: int) -> str:
    """Return text no longer than ``budget``, preserving a truncation notice."""
    text = text or ""
    budget = max(0, budget)
    if len(text) <= budget:
        return text
    if budget == 0:
        return ""
    if budget <= len(_TRUNCATION_SUFFIX):
        return _TRUNCATION_SUFFIX[-budget:]
    return text[: budget - len(_TRUNCATION_SUFFIX)] + _TRUNCATION_SUFFIX


def fit_parts_to_budget(
    parts: Sequence[tuple[str, str]],
    budget: int,
    floor: int = TRIM_FLOOR_CHARS,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Trim ``(name, text)`` parts largest-first to a strict text budget.

    The requested floor is honored when possible. If ``floor * part_count``
    itself exceeds the budget, a smaller per-part floor is derived so the
    function still keeps an excerpt from every source without violating its
    advertised cap.
    """
    budget = max(0, budget)
    fitted = [(str(name), str(text or "")) for name, text in parts]
    total = sum(len(text) for _, text in fitted)
    if total <= budget:
        return fitted, []

    original_lengths = [len(text) for _, text in fitted]
    effective_floor = min(max(0, floor), budget // len(fitted)) if fitted else 0

    while total > budget:
        candidates = [
            i for i, (_, text) in enumerate(fitted) if len(text) > effective_floor
        ]
        if not candidates:
            break
        idx = max(candidates, key=lambda i: len(fitted[i][1]))
        name, text = fitted[idx]
        keep = max(effective_floor, len(text) - (total - budget))
        fitted[idx] = (name, text[:keep])
        total -= len(text) - keep

    # Defensive final shave for unexpected edge cases (for example a negative
    # custom floor supplied by a caller). This loop is normally a no-op.
    for idx in sorted(range(len(fitted)), key=lambda i: len(fitted[i][1]), reverse=True):
        if total <= budget:
            break
        name, text = fitted[idx]
        cut = min(len(text), total - budget)
        fitted[idx] = (name, text[: len(text) - cut])
        total -= cut

    notes = [
        f"{fitted[i][0]}: kept {len(fitted[i][1]):,} of {original_lengths[i]:,} characters"
        for i in range(len(fitted))
        if len(fitted[i][1]) < original_lengths[i]
    ]
    return fitted, notes


def render_document_parts(
    parts: Sequence[tuple[str, str]],
    budget: int,
) -> tuple[str, list[str]]:
    """Render named document sections without exceeding ``budget`` characters."""
    budget = max(0, budget)
    normalized = [(str(name)[:500], str(text or "")) for name, text in parts]
    if not normalized or budget == 0:
        return "", []

    def render(values: Iterable[tuple[str, str]]) -> str:
        return "\n\n".join(f"=== {name} ===\n{text}" for name, text in values)

    untrimmed = render(normalized)
    if len(untrimmed) <= budget:
        return untrimmed, []

    heading_chars = sum(len(f"=== {name} ===\n") for name, _ in normalized)
    separator_chars = 2 * max(0, len(normalized) - 1)
    generic_note = (
        f"\n\n[PIPELINE NOTE: input trimmed to fit the model context window; "
        f"one or more of {len(normalized)} documents were truncated]"
    )
    text_budget = max(
        0,
        budget - heading_chars - separator_chars - len(generic_note),
    )
    fitted, notes = fit_parts_to_budget(normalized, text_budget)
    rendered = render(fitted)
    if notes:
        rendered += generic_note
    return truncate_text_to_budget(rendered, budget), notes


def limit_shared_context(shared_context: str | None) -> str | None:
    """Apply the shared-context portion of the total request budget."""
    if not shared_context:
        return None
    return truncate_text_to_budget(shared_context, MAX_SHARED_CONTEXT_CHARS)


def limit_item_title(title: str) -> str:
    """Bound title contribution to prompt and shared-context wrappers."""
    return (title or "")[:MAX_ITEM_TITLE_CHARS]


def prepare_item_text(
    title: str,
    text: str,
    shared_context: str | None = None,
    *,
    inline_shared: bool,
) -> str:
    """Compose or size an item's document input within the total request cap.

    When cached content is used, shared text is not repeated in the returned
    value, but its size is still reserved from the request's context budget.
    """
    title = limit_item_title(title)
    shared = limit_shared_context(shared_context)
    item_text = text or ""
    if not shared:
        return truncate_text_to_budget(item_text, MAX_ITEM_INPUT_CHARS)

    prefix = "=== SHARED CONTEXT (Background documents for this meeting) ===\n\n"
    separator = f"\n\n=== AGENDA ITEM: {title} ===\n\n"
    wrapper_chars = len(prefix) + len(separator) if inline_shared else 0
    item_budget = max(
        0,
        MAX_ITEM_INPUT_CHARS - len(shared) - wrapper_chars,
    )
    item_text = truncate_text_to_budget(item_text, item_budget)
    if inline_shared:
        return truncate_text_to_budget(
            f"{prefix}{shared}{separator}{item_text}",
            MAX_ITEM_INPUT_CHARS,
        )
    return item_text
