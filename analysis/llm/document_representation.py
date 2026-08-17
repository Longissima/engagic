"""Deterministic, information-preserving representations for oversized inputs.

This is intentionally a small model-input boundary, not another document
analysis product.  Original bytes and full extracted text remain authoritative
in the corpus.  These adapters only remove extraction/layout redundancy from
document shapes they can identify with high confidence; unsupported documents
remain raw and may still fail provider preflight.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from parsing.text_quality import is_garbled_text_layer


REPRESENTATION_VERSION = "civic-document-v2"

_PAGE_RE = re.compile(r"^--- PAGE (\d+) ---$")
_ADDED_RE = re.compile(r"^\[ADDED: (.*)\]$")
_DELETED_RE = re.compile(r"^\[DELETED: (.*)\]$")
_MULTISPACE_RE = re.compile(r"[ \t]{2,}")
_TOKEN_RE = re.compile(r"\S+")
_PAGE_SPLIT_RE = re.compile(r"(?m)^--- PAGE (\d+) ---\n?")
_NUMBER_CELL_RE = re.compile(r"(?:[$+\-]?[\d,.]+%?|N/?A|-)\Z", re.IGNORECASE)
_NEGATIVE_SCIENTIFIC_EXPONENT_RE = re.compile(r"(?<=\d)E-0([1-9])")
_AERMOD_RECORD_RE = re.compile(
    r"^(LOCATION|SRCPARAM|SRCGROUP|DISCCART|EVALCART|AREAVERT|BUILDHGT|"
    r"BUILDWID|BUILDLEN|XBADJ|YBADJ|HOUREMIS|EMISFACT)\b",
    re.IGNORECASE | re.MULTILINE,
)
_AERMOD_SIGNAL_RE = re.compile(
    r"\b(MAXIMUM|HIGHEST|AVERAGE\s+CONC|CONC\s+OF|CANCER\s+RISK|"
    r"HAZARD\s+INDEX|PM[_ .-]?(?:10|2[.]?5))\b",
    re.IGNORECASE,
)
_CONTRACT_CATALOG_HEADER = (
    "MFGPART           MFGNAME               PRODNAME           "
    "ISSCODE     SIN    PPOINT   GSAPRICE"
)


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """One corpus-backed source supplied to a representation adapter."""

    name: str
    text: str
    content_sha256: str | None = None
    source_url: str | None = None
    page_count: int | None = None
    document_format: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SourceDocument":
        return cls(
            name=str(value.get("name") or value.get("source_url") or "document"),
            text=str(value.get("text") or ""),
            content_sha256=(
                str(value["content_sha256"])
                if value.get("content_sha256")
                else None
            ),
            source_url=(str(value["source_url"]) if value.get("source_url") else None),
            page_count=(
                int(value["page_count"])
                if value.get("page_count") is not None
                else None
            ),
            document_format=(
                str(value["document_format"])
                if value.get("document_format")
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class DocumentRepresentation:
    """Rendered item input plus enough audit data to explain the transform."""

    text: str
    adapters: tuple[str, ...]
    source_chars: int
    represented_chars: int
    documents: int

    @property
    def compacted(self) -> bool:
        return any(adapter != "raw" for adapter in self.adapters)

    @property
    def ratio(self) -> float:
        return self.represented_chars / max(1, self.source_chars)


def _base36(number: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if number == 0:
        return "0"
    result = ""
    while number:
        number, remainder = divmod(number, 36)
        result = digits[remainder] + result
    return result


def _tagged_table_shape(text: str) -> bool:
    """Recognize table cells mislabeled as legislative redlines.

    The PDF extractor's geometric mark detector can see thousands of table
    rules as underlines.  A real amendment contains prose runs; these false
    positives are dominated by short, line-isolated numeric/cell values.
    """
    nonempty = 0
    tagged = 0
    short_tagged = 0
    numeric_tagged = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        nonempty += 1
        match = _ADDED_RE.fullmatch(line) or _DELETED_RE.fullmatch(line)
        if match is None:
            continue
        tagged += 1
        value = match.group(1).strip()
        short_tagged += len(value) <= 80
        numeric_tagged += bool(re.fullmatch(r"[$,().%+\-\d ]*", value))

    return (
        tagged >= 500
        and tagged / max(1, nonempty) >= 0.20
        and short_tagged / tagged >= 0.80
        and numeric_tagged / tagged >= 0.30
    )


def _fixed_width_table_shape(text: str) -> bool:
    """Recognize large fixed-width/numeric report output, not ordinary prose."""
    nonempty = 0
    table_lines = 0
    numeric_lines = 0
    repeated_token_lines = 0
    # A bounded sample is enough for classification and avoids a second full
    # traversal of multi-million-line election reports.
    for raw_line in text.splitlines()[:100_000]:
        line = raw_line.strip()
        if not line:
            continue
        nonempty += 1
        if len(_MULTISPACE_RE.findall(raw_line)) >= 2:
            table_lines += 1
        digits = sum(char.isdigit() for char in line)
        if digits >= 4 and digits / len(line) >= 0.20:
            numeric_lines += 1
        tokens = _TOKEN_RE.findall(line)
        if len(tokens) >= 8 and len(set(tokens)) <= len(tokens) // 2:
            repeated_token_lines += 1

    if nonempty < 200:
        return False
    return (
        table_lines / nonempty >= 0.20
        or numeric_lines / nonempty >= 0.45
        or repeated_token_lines >= 20
    )


def _election_canvass_shape(text: str) -> bool:
    return (
        "OFFICIAL CANVASS" in text.upper()
        and "PRECINCT RESULTS REPORT" in text.upper()
        and text.count("[ADDED:") >= 10_000
    )


def _aermod_shape(text: str) -> bool:
    return (
        text.count("*** AERMOD - VERSION") >= 25
        and len(_AERMOD_RECORD_RE.findall(text)) >= 500
    )


def _contract_catalog_shape(text: str) -> bool:
    return (
        "GENERAL SERVICES ADMINISTRATION" in text[:20_000]
        and text.count(_CONTRACT_CATALOG_HEADER) >= 25
    )


def _provider_blocked_evidence_shape(document: SourceDocument) -> bool:
    """Recognize a public exhibit known to trip provider-level prohibition."""
    return "rubmaps" in document.name.casefold() and len(document.text) >= 10_000


def needs_proactive_representation(
    documents: Iterable[SourceDocument | Mapping[str, Any]],
) -> bool:
    """Return whether provider compatibility requires adaptation before size preflight."""
    return any(
        _provider_blocked_evidence_shape(
            value
            if isinstance(value, SourceDocument)
            else SourceDocument.from_mapping(value)
        )
        for value in documents
    )


def _rle_tokens(line: str) -> str:
    """Run-length encode long adjacent token runs while preserving their count."""
    tokens = line.split()
    if len(tokens) < 6:
        return line
    output: list[str] = []
    index = 0
    changed = False
    while index < len(tokens):
        end = index + 1
        while end < len(tokens) and tokens[end] == tokens[index]:
            end += 1
        count = end - index
        if count >= 4:
            output.append(f"{tokens[index]}*{count}")
            changed = True
        else:
            output.extend(tokens[index:end])
        index = end
    return " ".join(output) if changed else line


def _packed_election_cells(values: Sequence[str]) -> list[str]:
    """Pack the canvass's label + five tally cells into explicit TSV rows."""
    if len(values) < 6 or len(values) % 6:
        return list(values)
    rows: list[str] = []
    for index in range(0, len(values), 6):
        row = values[index : index + 6]
        if _NUMBER_CELL_RE.fullmatch(row[0]) or not all(
            _NUMBER_CELL_RE.fullmatch(value) for value in row[1:]
        ):
            return list(values)
        rows.append("\t".join(row))
    return rows


def _compact_table_lines(
    text: str,
    *,
    strip_cell_tags: bool,
    pack_election_rows: bool = False,
) -> list[str]:
    """Normalize one table/report into a token-complete line stream."""
    output: list[str] = []
    in_cell_block = False
    cell_values: list[str] = []

    def close_cell_block() -> None:
        nonlocal in_cell_block, cell_values
        if not in_cell_block:
            return
        output.extend(
            _packed_election_cells(cell_values)
            if pack_election_rows
            else cell_values
        )
        output.append("@endcells")
        in_cell_block = False
        cell_values = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        page_match = _PAGE_RE.fullmatch(line)
        if page_match:
            close_cell_block()
            output.append(f"@page {page_match.group(1)}")
            continue

        tag_match = (
            (_ADDED_RE.fullmatch(line) or _DELETED_RE.fullmatch(line))
            if strip_cell_tags
            else None
        )
        if tag_match is not None:
            if not in_cell_block:
                output.append("@cells")
                in_cell_block = True
            # One line remains one source cell. Empty cells are explicit.
            cell_values.append(tag_match.group(1).strip() or "@empty")
            continue

        # Formatting-aware extraction places a blank paragraph separator
        # between adjacent marked table cells. It is extractor furniture, not
        # a row boundary; the one-cell-per-line rule retains the real order.
        if in_cell_block and not line:
            continue

        if in_cell_block:
            close_cell_block()

        if not line:
            if output and output[-1] != "":
                output.append("")
            continue

        # Two or more spaces are fixed-width column layout. A tab is both
        # shorter and clearer to the model while retaining the field boundary.
        line = _MULTISPACE_RE.sub("\t", line)
        output.append(_rle_tokens(line))

    if in_cell_block:
        close_cell_block()
    while output and output[-1] == "":
        output.pop()
    return output


def _split_source_pages(text: str) -> list[tuple[int, str]]:
    parts = _PAGE_SPLIT_RE.split(text)
    return [
        (int(parts[index]), parts[index + 1])
        for index in range(1, len(parts), 2)
    ]


def _render_reversible_table_stream(
    text: str,
    *,
    adapter: str,
    strip_cell_tags: bool,
) -> str:
    """Render table/layout text through the shared reversible token stream."""
    lines, repeated_coordinates, abbreviated_exponents = (
        _compact_numeric_table_notation(
            _compact_table_lines(
                text,
                strip_cell_tags=strip_cell_tags,
                pack_election_rows=False,
            )
        )
    )
    lines = _rle_repeated_lines(lines)
    encoded, definitions = _dictionary_encode(lines)
    preamble = [
        f"[REPRESENTATION {REPRESENTATION_VERSION}; adapter={adapter}]",
        "@page N = source page; tabs = source fixed-width field boundaries; VALUE*N = N adjacent identical tokens, cells, or rows.",
    ]
    if strip_cell_tags:
        preamble.append(
            "@cells..@endcells = line-isolated table cells in source order; one line is one cell; @empty is an empty cell."
        )
    if repeated_coordinates:
        preamble.append(
            "@xy within a row repeats that row's second and third fields exactly as 'X, Y'."
        )
    if abbreviated_exponents:
        preamble.append(
            "In numeric table fields, a lowercase eN suffix expands to E-0N (for example 1.2e6 = 1.2E-06)."
        )
    if definitions:
        preamble.append("~ aliases expand to these exact repeated source lines:")
        preamble.extend(f"{alias}\t{line}" for alias, line in definitions)
        preamble.append("@endaliases")
    preamble.append("[/REPRESENTATION]")
    return "\n".join([*preamble, *encoded])


def _compact_numeric_table_notation(
    lines: Sequence[str],
) -> tuple[list[str], int, int]:
    """Remove repeated numeric notation while retaining every table value.

    Large modeled-risk grids repeat each row's X/Y fields later in the same
    row and spell every small value with a token-heavy ``E-0N`` suffix.  Both
    transforms are deterministic and declared in the representation preamble;
    no row, column, precision digit, sign, or exponent is discarded.
    """
    output: list[str] = []
    repeated_coordinates = 0
    abbreviated_exponents = 0
    for source_line in lines:
        line = source_line
        fields = line.split("\t")
        if len(fields) >= 4:
            repeated_xy = f"{fields[1]}, {fields[2]}"
            if repeated_xy in line:
                line = line.replace(repeated_xy, "@xy", 1)
                repeated_coordinates += 1

        line, substitutions = _NEGATIVE_SCIENTIFIC_EXPONENT_RE.subn(r"e\1", line)
        abbreviated_exponents += substitutions
        output.append(line)
    return output, repeated_coordinates, abbreviated_exponents


def _render_aermod_receipt(document: SourceDocument) -> str:
    """Keep authored findings; compact tables and receipt the model appendix."""
    pages = _split_source_pages(document.text)
    first_model_index = next(
        (
            index
            for index, (_, body) in enumerate(pages)
            if "SO STARTING" in body
            or sum(
                bool(_AERMOD_RECORD_RE.match(line.strip()))
                for line in body.splitlines()
            )
            >= 10
        ),
        None,
    )
    if first_model_index is None:
        return document.text

    authored_pages = pages[:first_model_index]
    model_pages = pages[first_model_index:]
    authored_text = "\n\n".join(
        f"--- PAGE {page_num} ---\n{body.rstrip()}"
        for page_num, body in authored_pages
    )
    authored_table_adapter = None
    if _fixed_width_table_shape(authored_text):
        authored_table_adapter = "aermod-authored-fixed-width-v1"
        authored_text = _render_reversible_table_stream(
            authored_text,
            adapter=authored_table_adapter,
            strip_cell_tags=_tagged_table_shape(authored_text),
        )
    appendix_text = "\n\n".join(
        f"--- PAGE {page_num} ---\n{body.rstrip()}"
        for page_num, body in model_pages
    )

    families: dict[str, dict[str, Any]] = {}
    engine_headers: Counter[str] = Counter()
    output_signals: Counter[str] = Counter()
    for raw_line in appendix_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "*** AERMOD - VERSION" in line or "*** AERMET - VERSION" in line:
            engine_headers[re.sub(r"\s+", " ", line)] += 1
        if _AERMOD_SIGNAL_RE.search(line) and len(line) <= 240:
            output_signals[re.sub(r"\s+", " ", line)] += 1

        match = _AERMOD_RECORD_RE.match(line)
        if match is None:
            continue
        family = match.group(1).upper()
        stats = families.setdefault(family, {"count": 0, "columns": []})
        stats["count"] += 1
        numeric_values: list[Decimal] = []
        for token in line.split()[1:]:
            cleaned = token.strip("(),;*")
            try:
                numeric_values.append(Decimal(cleaned))
            except InvalidOperation:
                continue
        columns: list[list[Decimal]] = stats["columns"]
        while len(columns) < len(numeric_values):
            columns.append([])
        for index, number in enumerate(numeric_values):
            columns[index].append(number)

    receipt = [
        f"[REPRESENTATION {REPRESENTATION_VERSION}; adapter=aermod-report-receipt-v1]",
        (
            "The authored air-quality report above retains every row and field in the shared reversible fixed-width representation. "
            if authored_table_adapter
            else "The authored air-quality report above is verbatim. "
        )
        + "The appended AERMOD/AERMET machine dump is represented by a deterministic audit receipt; its complete extraction remains in the corpus source named in PROVENANCE.",
        f"authored_adapter={authored_table_adapter or 'raw-verbatim'}",
        f"appendix_pages={model_pages[0][0]}-{model_pages[-1][0]}",
        f"appendix_page_count={len(model_pages)}",
        f"appendix_chars={len(appendix_text)}",
        f"appendix_lines={len(appendix_text.splitlines())}",
        f"appendix_sha256={hashlib.sha256(appendix_text.encode()).hexdigest()}",
        "@record_families count and numeric-column ranges (column order is source order)",
    ]
    for family, stats in sorted(families.items()):
        ranges = []
        for values in stats["columns"]:
            ranges.append(f"{min(values)}..{max(values)}")
        receipt.append(f"{family}\t{stats['count']}\t" + "\t".join(ranges))

    if engine_headers:
        receipt.append("@engine_headers exact line\toccurrences")
        receipt.extend(
            f"{line}\t{count}" for line, count in sorted(engine_headers.items())
        )
    if output_signals:
        receipt.append("@model_output_signals exact line\toccurrences")
        receipt.extend(
            f"{line}\t{count}"
            for line, count in sorted(output_signals.items())[:2_000]
        )
    receipt.append("[/REPRESENTATION]")
    return authored_text + "\n\n" + "\n".join(receipt)


def _render_contract_catalog_receipt(document: SourceDocument) -> str:
    """Preserve contract terms and receipt the many-thousand-row SKU catalog."""
    first_header = document.text.find(_CONTRACT_CATALOG_HEADER)
    if first_header < 0:
        return document.text
    prefix = document.text[:first_header].rstrip()
    catalog_text = document.text[first_header:]
    header = _CONTRACT_CATALOG_HEADER.lstrip()
    rows: list[str] = []
    parsed: list[tuple[str, Decimal, str]] = []
    noncatalog_lines: list[tuple[int, str]] = []
    price_re = re.compile(
        r"\s(?P<sin>(?:\d{4,9}[A-Z]*|ANCILLARY|OLM))\s+"
        r"(?P<point>[A-Z]{2})\s+(?P<price>\d[\d,]*[.]\d{2})\s*$"
    )
    for source_line, raw_line in enumerate(catalog_text.splitlines(), start=1):
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped == header or _PAGE_RE.fullmatch(stripped):
            continue
        price_match = price_re.search(line)
        if price_match is None:
            # Catalogs can contain amendments, clauses, explanatory prose, and
            # wrapped rows after the first repeated header. Those lines are
            # primary evidence, not catalog furniture: retain them verbatim
            # with their source line so the row receipt never hides a decision.
            noncatalog_lines.append((source_line, line))
            continue
        price = Decimal(price_match.group("price").replace(",", ""))
        rows.append(stripped)
        parsed.append((price_match.group("sin"), price, stripped))

    if len(parsed) < 1_000:
        return document.text

    sin_stats: dict[str, list[Decimal]] = {}
    for sin, price, _ in parsed:
        sin_stats.setdefault(sin, []).append(price)

    ordered = sorted(parsed, key=lambda row: (row[1], row[2]))
    receipt = [
        f"[REPRESENTATION {REPRESENTATION_VERSION}; adapter=contract-catalog-receipt-v1]",
        "Contract text outside parsed SKU rows is retained verbatim above or in @noncatalog_lines below. The item-level GSA price schedule is represented by a deterministic catalog receipt; every SKU row remains in the corpus source named in PROVENANCE.",
        "schema=MFGPART|MFGNAME|PRODNAME|ISSCODE|SIN|PPOINT|GSAPRICE",
        f"catalog_rows={len(parsed)}",
        f"unparsed_nonblank_lines={len(noncatalog_lines)}",
        f"catalog_rows_sha256={hashlib.sha256(chr(10).join(rows).encode()).hexdigest()}",
        f"price_range_usd={ordered[0][1]}..{ordered[-1][1]}",
        "manufacturer_field=retained_in_corpus_rows_but_not_aggregated_because_the_PDF_fixed_width_column_overlaps_long_part_numbers",
    ]
    receipt.append("@special_item_numbers SIN\trows\tmin_price\tmax_price")
    receipt.extend(
        f"{sin}\t{len(prices)}\t{min(prices)}\t{max(prices)}"
        for sin, prices in sorted(sin_stats.items())
    )
    receipt.append("@lowest_price_rows exact source rows")
    receipt.extend(row[2] for row in ordered[:25])
    receipt.append("@highest_price_rows exact source rows")
    receipt.extend(row[2] for row in ordered[-25:])
    if noncatalog_lines:
        receipt.append("@noncatalog_lines source_line\tverbatim source text")
        receipt.extend(
            f"{source_line}\t{line}"
            for source_line, line in noncatalog_lines
        )
        receipt.append("@end_noncatalog_lines")
    receipt.append("[/REPRESENTATION]")
    return prefix + "\n\n" + "\n".join(receipt)


def _render_election_canvass_receipt(document: SourceDocument) -> str:
    """Keep countywide/audit reports and receipt redundant precinct detail."""
    pages = _split_source_pages(document.text)
    precinct_start = next(
        (
            index
            for index, (page_num, body) in enumerate(pages)
            if page_num > 4 and "Precinct Results Report" in body
        ),
        None,
    )
    supplemental_start = next(
        (
            index
            for index, (page_num, body) in enumerate(pages)
            if page_num > 4 and "Provisional Ballot Report" in body
        ),
        None,
    )
    if (
        precinct_start is None
        or supplemental_start is None
        or supplemental_start <= precinct_start
    ):
        return document.text

    detail_pages = pages[precinct_start:supplemental_start]
    retained_pages = pages[:precinct_start] + pages[supplemental_start:]
    detail_text = "\n\n".join(
        f"--- PAGE {page_num} ---\n{body.rstrip()}"
        for page_num, body in detail_pages
    )
    precincts: Counter[str] = Counter()
    contests: Counter[str] = Counter()
    tagged_cells = 0
    for _, body in detail_pages:
        lines = [line.strip() for line in body.splitlines()]
        tagged_values = [
            match.group(1).strip()
            for line in lines
            if (match := _ADDED_RE.fullmatch(line)) is not None
        ]
        tagged_cells += len(tagged_values)
        vote_index = next(
            (index for index, line in enumerate(lines) if line.startswith("Vote For ")),
            None,
        )
        if vote_index is not None:
            before_vote = [
                value
                for line in lines[:vote_index]
                if (match := _ADDED_RE.fullmatch(line)) is not None
                and (value := match.group(1).strip())
            ]
            if before_vote:
                precincts[before_vote[-1]] += 1
            contest = next(
                (
                    line
                    for line in lines[vote_index + 1 :]
                    if line and _ADDED_RE.fullmatch(line) is None
                ),
                None,
            )
            if contest:
                contests[contest] += 1

    receipt = [
        "@canvass_detail_receipt",
        "Countywide summary results, certification, and post-canvass audit/supplemental reports are retained above/below. Repeated precinct and statement-of-votes pages are represented by this deterministic receipt; their complete extraction remains in the corpus source named in PROVENANCE.",
        f"detail_pages={detail_pages[0][0]}-{detail_pages[-1][0]}",
        f"detail_page_count={len(detail_pages)}",
        f"detail_chars={len(detail_text)}",
        f"detail_tagged_cells={tagged_cells}",
        f"detail_sha256={hashlib.sha256(detail_text.encode()).hexdigest()}",
        "@precincts exact label\tpage_occurrences",
    ]
    receipt.extend(
        f"{name}\t{count}" for name, count in sorted(precincts.items())
    )
    receipt.append("@contests exact label\tpage_occurrences")
    receipt.extend(
        f"{name}\t{count}" for name, count in sorted(contests.items())
    )
    receipt.append("@end_canvass_detail_receipt")

    retained_text = "\n\n".join(
        f"--- PAGE {page_num} ---\n{body.rstrip()}"
        for page_num, body in retained_pages
    )
    lines = _rle_repeated_lines(
        _compact_table_lines(
            retained_text,
            strip_cell_tags=True,
            pack_election_rows=True,
        )
    )
    encoded, definitions = _dictionary_encode([*receipt, *lines])
    preamble = [
        f"[REPRESENTATION {REPRESENTATION_VERSION}; adapter=election-canvass-receipt-v1]",
        "@page N = source page; tabs preserve source tally fields; six-field @cells lines are label, total, early, election_day, provisional, late_early.",
    ]
    if definitions:
        preamble.append("~ aliases expand to these exact repeated source lines:")
        preamble.extend(f"{alias}\t{line}" for alias, line in definitions)
        preamble.append("@endaliases")
    preamble.append("[/REPRESENTATION]")
    return "\n".join([*preamble, *encoded])


def _render_provider_blocked_evidence_receipt(document: SourceDocument) -> str:
    """Receipt an explicit third-party review exhibit without deleting it."""
    lowered = document.text.casefold()
    return "\n".join(
        [
            f"[REPRESENTATION {REPRESENTATION_VERSION}; adapter=public-record-evidence-receipt-v1]",
            "This is a public licensing-record exhibit compiling third-party online reviews of a massage business. Its complete original and extracted text remain in the corpus source named in PROVENANCE; the provider rejected the verbatim exhibit as prohibited content, so the model receives this deterministic audit receipt.",
            f"source_chars={len(document.text)}",
            f"source_lines={len(document.text.splitlines())}",
            f"source_pages={document.text.count('--- PAGE ')}",
            f"text_sha256={hashlib.sha256(document.text.encode()).hexdigest()}",
            f"review_mentions={lowered.count('review')}",
            f"massage_mentions={lowered.count('massage')}",
            f"rating_mentions={lowered.count('rating')}",
            f"provider_mentions={lowered.count('provider')}",
            "[/REPRESENTATION]",
        ]
    )


def _dictionary_encode(lines: Sequence[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Alias exact repeated lines when the definition produces net savings."""
    counts = Counter(
        line for line in lines if len(line) >= 8 and not line.startswith(("@", "~"))
    )
    candidates = sorted(
        (
            (line, count)
            for line, count in counts.items()
            if count >= 4
        ),
        key=lambda pair: (-(len(pair[0]) * (pair[1] - 1)), pair[0]),
    )

    aliases: dict[str, str] = {}
    definitions: list[tuple[str, str]] = []
    for line, count in candidates[:512]:
        alias = f"~{_base36(len(definitions))}"
        # Definition: alias, tab, value, newline. Replacements each keep a
        # newline too, so compare only the changed line payload.
        savings = count * (len(line) - len(alias)) - (len(alias) + len(line) + 1)
        if savings <= 0:
            continue
        aliases[line] = alias
        definitions.append((alias, line))

    return [aliases.get(line, line) for line in lines], definitions


def _rle_repeated_lines(lines: Sequence[str]) -> list[str]:
    """Run-length encode adjacent identical cells/rows without dropping count."""
    output: list[str] = []
    index = 0
    while index < len(lines):
        end = index + 1
        while end < len(lines) and lines[end] == lines[index]:
            end += 1
        count = end - index
        line = lines[index]
        if count >= 4 and line and not line.startswith(("@", "~")):
            output.append(f"{line}*{count}")
        else:
            output.extend(lines[index:end])
        index = end
    return output


def _render_compact_document(document: SourceDocument) -> tuple[str, str]:
    if is_garbled_text_layer(document.text):
        return document.text, "raw"
    if _provider_blocked_evidence_shape(document):
        return (
            _render_provider_blocked_evidence_receipt(document),
            "public-record-evidence-receipt-v1",
        )
    if _aermod_shape(document.text):
        return _render_aermod_receipt(document), "aermod-report-receipt-v1"
    if _contract_catalog_shape(document.text):
        return (
            _render_contract_catalog_receipt(document),
            "contract-catalog-receipt-v1",
        )
    if _election_canvass_shape(document.text):
        return (
            _render_election_canvass_receipt(document),
            "election-canvass-receipt-v1",
        )
    strip_cell_tags = _tagged_table_shape(document.text)
    fixed_width = _fixed_width_table_shape(document.text)
    if not strip_cell_tags and not fixed_width:
        return document.text, "raw"

    adapter = "tagged-table-v1" if strip_cell_tags else "fixed-width-report-v1"
    return (
        _render_reversible_table_stream(
            document.text,
            adapter=adapter,
            strip_cell_tags=strip_cell_tags,
        ),
        adapter,
    )


def render_documents(documents: Iterable[SourceDocument | Mapping[str, Any]]) -> str:
    """Render full extracted sources without a model-window truncation step."""
    normalized = [
        value if isinstance(value, SourceDocument) else SourceDocument.from_mapping(value)
        for value in documents
    ]
    return "\n\n".join(
        f"=== {document.name} ===\n{document.text}" for document in normalized
    )


def build_compact_representation(
    documents: Iterable[SourceDocument | Mapping[str, Any]],
) -> DocumentRepresentation:
    """Compact recognized sources and leave every unsupported source raw."""
    normalized = [
        value if isinstance(value, SourceDocument) else SourceDocument.from_mapping(value)
        for value in documents
    ]
    rendered: list[str] = []
    adapters: list[str] = []
    source_chars = 0

    for document in normalized:
        compact_text, adapter = _render_compact_document(document)
        source_chars += len(document.text)
        adapters.append(adapter)
        provenance = [f"name={document.name}"]
        if document.content_sha256:
            provenance.append(f"sha256={document.content_sha256}")
        if document.source_url:
            provenance.append(f"source={document.source_url}")
        if document.page_count is not None:
            provenance.append(f"pages={document.page_count}")
        if document.document_format:
            provenance.append(f"format={document.document_format}")
        rendered.append(
            "\n".join(
                [
                    f"=== {document.name} ===",
                    "[PROVENANCE " + "; ".join(provenance) + "]",
                    compact_text,
                ]
            )
        )

    text = "\n\n".join(rendered)
    return DocumentRepresentation(
        text=text,
        adapters=tuple(adapters),
        source_chars=source_chars,
        represented_chars=len(text),
        documents=len(normalized),
    )
