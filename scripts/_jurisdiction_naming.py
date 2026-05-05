"""Shared naming helpers for jurisdiction bananas.

Used by db_viewer (interactive add) and boardbook_bulk_import (batch add).
Single source of truth for the `<stem>sd<STATE>` school-district convention.
"""

import re
import unicodedata


def to_banana_slug(name: str) -> str:
    """ASCII-fold + strip non-alphanumeric. Diacritics survive as base letters
    (La Canada -> lacanada, not lacaada).
    """
    normalized = unicodedata.normalize("NFKD", name)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-zA-Z0-9]", "", ascii_only).lower()


# Trailing words to strip when reducing a district name to its stem.
# Iteratively peeled from the right -- "Joint Unified School District" goes
# in three passes. Order doesn't affect correctness because we test for the
# longest match per pass.
_DISTRICT_SUFFIXES = (
    "consolidated school district",
    "independent school district",
    "joint unified school district",
    "unified school district",
    "regional school district",
    "central school district",
    "public schools",
    "school district",
    "city schools",
    "schools",
    "consolidated",
    "independent",
    "regional",
    "unified",
    "central",
    "joint",
    "isd",
    "cisd",
    "usd",
    "csd",
    "psd",
    "rsd",
)

# MN/IL convention: "Independent School District [No.] [#]NNN [optional town]".
# The number is the unique part; town (if any) follows. Strip the prefix so
# the remaining text becomes the stem, falling back to "isd<NNN>" if the name
# is purely numeric.
_NUMBERED_PREFIX_RE = re.compile(
    r"^(?:independent\s+school\s+district|school\s+district)"
    r"(?:\s+(?:no\.?|number|#))?\s*#?\s*\d+(?:-\d+)?\s*",
    re.IGNORECASE,
)
_TRAILING_NUM_RE = re.compile(r"\s+#?[\d\-]+$")
_ANY_NUM_RE = re.compile(r"\d+(?:-\d+)?")

# Single-letter acronym dots: 'C.I.S.D.' -> 'CISD', 'I.S.D' -> 'ISD'.
# Some directory listings (BoardBook included) render district acronyms with
# periods, which prevent suffix-strip rules from matching.
_ACRONYM_DOTS_RE = re.compile(r"\b((?:[A-Za-z]\.){2,})")


def _strip_acronym_dots(s: str) -> str:
    return _ACRONYM_DOTS_RE.sub(lambda m: m.group(1).replace(".", ""), s)


# Parenthetical disambiguator like "Wylie ISD (Collin County)" or
# "Rocksprings ISD (069901)". Alphabetic content becomes a banana tag
# (county name etc.); numeric content is treated as bookkeeping noise.
_PAREN_RE = re.compile(r"\s*\(([^)]+)\)\s*")
_PAREN_QUALIFIER_NOISE_RE = re.compile(
    r"\b(county|parish|borough|district|isd|cisd|usd)\b", re.IGNORECASE
)


def _extract_paren_tag(name: str) -> tuple[str, str]:
    """Return (name_with_parens_removed, banana_tag).

    Numeric paren content (e.g. '069901' TEA codes) is dropped entirely.
    Alphabetic content is slugged with common qualifier words removed
    ('Collin County' -> 'collin'), to be appended after the stem.
    """
    m = _PAREN_RE.search(name)
    if not m:
        return name, ""
    inner = m.group(1)
    cleaned = (name[: m.start()] + name[m.end():]).strip()
    digit_count = sum(c.isdigit() for c in inner)
    if digit_count > len(inner) / 2:
        return cleaned, ""
    stripped = _PAREN_QUALIFIER_NOISE_RE.sub("", inner)
    return cleaned, to_banana_slug(stripped)


def derive_district_stem(name: str) -> str:
    """Reduce a school-district name to a short stem suitable for a banana.

    Examples:
      'Prosper Independent School District'                  -> 'prosper'
      'Los Angeles Unified School District'                  -> 'losangeles'
      'Fairfax County Public Schools'                        -> 'fairfaxcounty'
      'Pierre School District 32-2'                          -> 'pierre'
      'Independent School District #272 Eden Prairie Schools'-> 'edenprairie'
      'Independent School District 748'                      -> 'isd748'
      'School District 45'                                   -> 'isd45'

    Strategy:
      1. Strip a leading "(Independent) School District [No.] NNN" prefix.
      2. Strip trailing district numbers ("32-2", "#465").
      3. Iteratively peel district-suffix words from the right -- only when
         preceded by a space, never when they consume the entire string
         (so 'Central Public Schools' reduces to 'central', not '').
      4. If the resulting slug is empty, fall back to 'isd<number>' using
         any digit run from the original name.
    """
    normalized = _strip_acronym_dots(name.strip())
    cleaned, paren_tag = _extract_paren_tag(normalized)
    s = _NUMBERED_PREFIX_RE.sub("", cleaned).strip()
    s = _TRAILING_NUM_RE.sub("", s).strip()

    while True:
        lower = s.lower()
        for suf in _DISTRICT_SUFFIXES:
            if lower.endswith(" " + suf):
                s = s[: -len(suf)].rstrip()
                break
        else:
            break

    stem = to_banana_slug(s)
    if stem:
        return stem + paren_tag

    # Fallback: name was purely numeric / consumed by stripping. Use the
    # district number as the stem with an "isd" prefix to keep it readable.
    m = _ANY_NUM_RE.search(cleaned)
    if m:
        return "isd" + m.group(0).replace("-", "") + paren_tag
    return paren_tag


# Trailing district number ("128", "32-2", "#465") at the end of the name.
_TRAILING_NUM_AT_END_RE = re.compile(r"#?(\d+(?:-\d+)?)\s*$")


def trailing_number(name: str) -> str:
    """Trailing district number, or '' if none.

    'Skokie School District 68'           -> '68'
    'Pierre School District 32-2'         -> '322'
    'Independent School District #465'    -> '465'
    'Prosper Independent School District' -> ''
    """
    m = _TRAILING_NUM_AT_END_RE.search(name.strip())
    return m.group(1).replace("-", "") if m else ""


def specific_infix_class(name: str) -> str:
    """Wrapper-word classification for collision disambiguation.

    Returns one of: 'sd' (default -- ISD, Independent SD, plain "School District"),
    'csd' (Consolidated, CISD), 'usd' (Unified, USD), 'psd' (Public Schools).

    Applied only when a banana collides; default-class names keep the simple
    `<stem>sd<STATE>` form, while non-default classes substitute their specific
    infix (csd/usd/psd) so two same-stem districts in the same state become
    distinct bananas without needing a number.
    """
    n = name.lower()
    if "cisd" in n or "consolidated" in n:
        return "csd"
    if "usd" in n or "unified" in n:
        return "usd"
    if "public schools" in n:
        return "psd"
    return "sd"


def disambiguate_bananas(
    rows: list,
    *,
    name_key: str = "name",
    state_key: str = "state",
    banana_key: str = "banana",
) -> set[str]:
    """Resolve banana collisions in `rows` (list of dicts) in place.

    Strategy:
      - Group rows by their initial banana.
      - For groups of size > 1, split by `specific_infix_class`.
      - A class with one member uses `<stem><class><STATE>` (no number);
        the default 'sd' class therefore retains the simple banana.
      - A class with multiple members uses `<stem><number><class><STATE>`;
        a member without a trailing number keeps the empty version, so
        e.g. 'City SD' stays at `citysdTX` while 'City SD 2' becomes `city2sdTX`.

    Returns the set of original collision bananas that are no longer claimed
    by any row -- callers should DELETE those orphans from storage.
    """
    from collections import defaultdict

    by_banana: dict[str, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        by_banana[row[banana_key]].append(i)

    orphans: set[str] = set()
    for original_banana, indices in by_banana.items():
        if len(indices) <= 1:
            continue

        by_class: dict[str, list[int]] = defaultdict(list)
        for i in indices:
            by_class[specific_infix_class(rows[i][name_key])].append(i)

        new_bananas: set[str] = set()
        for cls, idxs in by_class.items():
            multi = len(idxs) > 1
            for i in idxs:
                row = rows[i]
                stem = derive_district_stem(row[name_key])
                num = trailing_number(row[name_key]) if multi else ""
                new_banana = stem + num + cls + row[state_key]
                row[banana_key] = new_banana
                new_bananas.add(new_banana)

        if original_banana not in new_bananas:
            orphans.add(original_banana)

    return orphans
