"""Conservative text-layer quality signals shared by extraction and corpus."""

from __future__ import annotations


def is_garbled_text_layer(text: str) -> bool:
    """Return True for strong broken-font/CMap output, not merely sparse text.

    Broken PDF character maps commonly produce C1 control characters or pages
    dominated by extended glyphs and punctuation.  Numeric tables and normal
    non-English prose deliberately do not trip a single weak signal.
    """
    sample = (text or "").strip()
    if len(sample) < 200:
        return False

    total = len(sample)
    controls = sum(
        1
        for char in sample
        if (ord(char) < 32 and char not in "\n\r\t") or 0x7F <= ord(char) <= 0x9F
    )
    if controls / total >= 0.01:
        return True

    ascii_letters = sum(char.isascii() and char.isalpha() for char in sample)
    ascii_digits = sum(char.isascii() and char.isdigit() for char in sample)
    all_letters = sum(char.isalpha() for char in sample)
    nonspace = sum(not char.isspace() for char in sample)

    # A multilingual page still tends to be made of Unicode letters. Broken
    # CMaps are instead mostly symbols, with only a few extended glyphs that
    # happen to satisfy ``isalpha``. Keep script-neutral letter density as an
    # independent guard before using the ASCII-oriented corruption signal.
    readable_ratio = (ascii_letters + ascii_digits) / max(1, nonspace)
    letter_or_digit_ratio = (all_letters + ascii_digits) / max(1, nonspace)
    extended_letter_ratio = (all_letters - ascii_letters) / max(1, all_letters)
    return (
        readable_ratio < 0.20
        and letter_or_digit_ratio < 0.35
        and extended_letter_ratio >= 0.35
    )
