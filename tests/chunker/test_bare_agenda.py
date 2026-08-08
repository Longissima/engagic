"""Item bodies stop where the next item starts, and bare agendas keep their tail.

Regression pin for the Hillsboro OR planning-commission retreat (2026-08-08):
a one-page agenda whose headings render as a bare label line ("A.") followed by
a separate title line. Every item's body came out as the NEXT item's label,
which the summarizer then read as that item's content and wrote paragraphs
about. The last item, having no successor to mis-slice, was dropped entirely.

Drives _parse_agenda_items on synthetic lines rather than a rendered PDF: the
bug lives in line-index arithmetic, and PDF text layout is not reproducible
enough to pin it.
"""

import pytest

from vendors.adapters.parsers.agenda_chunker import _ParsedAgenda, _parse_agenda_items


LETTERS = ["A", "B", "C", "D", "E", "F", "G"]
TITLES = [
    "Target Area Planning and Land Use Implementation",
    "Development Trends and Impacts",
    "Urban Design Projects: Station 9, Police HQ",
    "Abstentions and Public Process",
    "Department Workplan Highlights",
    "Legislative Update and Impacts to Local Processes",
    "Data Centers",
]


def line(text: str, y: float, x: float = 72.0, bold: bool = False) -> dict:
    return {
        "text": text,
        "bbox": [x, y, x + 400, y + 12],
        "y0": y,
        "y1": y + 12,
        "x0": x,
        "is_bold": bold,
        "font_size": 11.0,
        "font_name": "Helvetica",
        "page": 0,
    }


def split_label_lines() -> list:
    """Label and title on separate lines -- the shape that triggered the bug."""
    lines = []
    y = 72.0
    for letter, title in zip(LETTERS, TITLES):
        lines.append(line(f"{letter}.", y))
        lines.append(line(title, y + 14, x=180.0))
        y += 40
    return lines


def inline_label_lines() -> list:
    """Label and title on one line -- the common shape, must not regress.

    Bold because a mixed-case single-letter item is only recognized as a
    header when the line is bold; that gate is not what these tests are about.
    """
    return [
        line(f"{letter}.      {title}", 72.0 + 30 * i, bold=True)
        for i, (letter, title) in enumerate(zip(LETTERS, TITLES))
    ]


@pytest.fixture
def result():
    return _ParsedAgenda()


@pytest.mark.parametrize(
    "make_lines", [split_label_lines, inline_label_lines], ids=["split", "inline"]
)
def test_body_never_contains_another_items_label(make_lines, result):
    items, _ = _parse_agenda_items(make_lines(), [], result)

    assert [item.number for item in items] == LETTERS
    for item in items:
        for letter in LETTERS:
            assert f"{letter}." not in item.body, (
                f"item {item.number} body carries item {letter}'s label: {item.body!r}"
            )


@pytest.mark.parametrize(
    "make_lines", [split_label_lines, inline_label_lines], ids=["split", "inline"]
)
def test_agenda_with_no_text_under_headings_yields_empty_bodies(make_lines, result):
    items, _ = _parse_agenda_items(make_lines(), [], result)

    assert all(not item.body for item in items)


def test_trailing_item_survives(result):
    """The item with nothing after it is the one the old slice deleted."""
    items, _ = _parse_agenda_items(split_label_lines(), [], result)

    assert items[-1].number == "G"
    assert items[-1].title == "Data Centers"


def test_real_body_text_is_still_collected(result):
    """The fix trims the next item's header, not this item's content."""
    lines = [
        line("A.      Target Area Planning and Land Use Implementation", 72.0, bold=True),
        line("Staff recommends adopting the target area boundary as mapped.", 86.0),
        line("Estimated cost is $1.2 million over three fiscal years.", 100.0),
        line("B.      Development Trends and Impacts", 130.0, bold=True),
        line("Annual review of permit volume against the 2040 growth forecast.", 144.0),
    ]

    items, _ = _parse_agenda_items(lines, [], result)

    assert items[0].number == "A"
    assert "Staff recommends adopting" in items[0].body
    assert "$1.2 million" in items[0].body
    assert "B." not in items[0].body
    assert "Development Trends" not in items[0].body
    assert "Annual review" in items[1].body
