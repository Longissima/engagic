"""A rung that returns one item for a long document must not win the cascade."""

import fitz

from vendors.adapters.parsers import router


def _long_pdf(tmp_path, pages=20):
    document = fitz.open()
    for index in range(pages):
        page = document.new_page()
        page.insert_text((72, 100), f"Page {index + 1} agenda text " * 10, fontsize=10)
    path = tmp_path / "packet.pdf"
    path.write_bytes(document.tobytes())
    document.close()
    return str(path)


def test_single_item_blob_falls_through_to_next_rung(tmp_path, monkeypatch):
    path = _long_pdf(tmp_path)
    calls = []

    def v2(pdf_path, force_method=None):
        calls.append("v2")
        return {"items": [{"title": "01", "body_text": "x" * 869_000}], "metadata": {"parse_method": "v2_toc"}}

    def v1(pdf_path, force_method=None):
        calls.append("v1")
        return {
            "items": [
                {"title": "Proclamations", "agenda_number": "1", "body_text": "a"},
                {"title": "Consent Calendar", "agenda_number": "4", "body_text": "b"},
                {"title": "Public Hearing", "agenda_number": "5", "body_text": "c"},
            ],
            "metadata": {"parse_method": "url"},
        }

    monkeypatch.setitem(router._ENGINE_FUNCS, "v2", v2)
    monkeypatch.setitem(router._ENGINE_FUNCS, "v1", v1)
    monkeypatch.setattr(router.config, "CHUNKER_CLASSIFIER_HINTS", False, raising=False)

    result = router.chunk_pdf(path, ["v2:auto", "v1:auto"], None)

    assert calls == ["v2", "v1"]
    assert result.winning_rung == "v1:auto"
    assert len(result.items) == 3
    assert result.attempts[0].failure_reason == router.DEGENERATE


def test_single_item_on_short_notice_is_still_a_win(tmp_path, monkeypatch):
    document = fitz.open()
    document.new_page().insert_text((72, 100), "Notice of cancellation", fontsize=12)
    path = tmp_path / "notice.pdf"
    path.write_bytes(document.tobytes())
    document.close()

    monkeypatch.setitem(
        router._ENGINE_FUNCS,
        "v2",
        lambda pdf_path, force_method=None: {
            "items": [{"title": "Notice of cancellation", "body_text": "short"}],
            "metadata": {"parse_method": "v2_toc"},
        },
    )
    monkeypatch.setattr(router.config, "CHUNKER_CLASSIFIER_HINTS", False, raising=False)
    result = router.chunk_pdf(str(path), ["v2:auto"], None)
    assert result.winning_rung == "v2:auto"
    assert len(result.items) == 1


def test_assessment_rows_do_not_win_cascade(tmp_path, monkeypatch):
    path = _long_pdf(tmp_path)
    rows = [{'title': f'00 2026 {2812340010170 + i} F 2800',
             'agenda_number': '42.00'} for i in range(900)]
    monkeypatch.setitem(router._ENGINE_FUNCS, 'v2', lambda *a, **k: {'items': rows})
    monkeypatch.setitem(router._ENGINE_FUNCS, 'v1', lambda *a, **k: {'items': []})
    monkeypatch.setattr(router.config, 'CHUNKER_CLASSIFIER_HINTS', False, raising=False)
    result = router.chunk_pdf(path, ['v2:auto', 'v1:auto'], None)
    assert result.items == []
    assert result.winning_rung is None
    assert result.attempts[0].failure_reason == router.NUMERIC_TABLE


def test_decimal_agenda_numbers_and_financial_titles_remain_valid():
    items = [{'agenda_number': f'{i}.00', 'title': 'Approve 2026 budget of $123456789'}
             for i in range(20)]
    assert not router._is_numeric_table(items)
