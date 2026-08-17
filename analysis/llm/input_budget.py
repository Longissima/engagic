"""Shared document-input assembly for streaming and batch summarization.

Character counts are useful for memory planning, but are not a safe model-
window authority. Provider token counting plus compatible deterministic
representations own oversized handling; these helpers no longer discard text.
"""

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

def limit_shared_context(shared_context: str | None) -> str | None:
    """Normalize an optional shared context without truncating source text."""
    return shared_context or None


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
    """Compose an item's complete document input without character trimming."""
    title = limit_item_title(title)
    shared = limit_shared_context(shared_context)
    item_text = text or ""
    if not shared:
        return item_text

    prefix = "=== SHARED CONTEXT (Background documents for this meeting) ===\n\n"
    separator = f"\n\n=== AGENDA ITEM: {title} ===\n\n"
    if inline_shared:
        return f"{prefix}{shared}{separator}{item_text}"
    return item_text
