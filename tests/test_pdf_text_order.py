from pathlib import Path

import fitz

from parsing.pdf import _extract_plain_page_text
from parsing.text_quality import has_excessive_layout_padding


def test_stormlake_memo_keeps_readable_native_order():
    with fitz.open(Path(__file__).parent / 'fixtures/stormlake_budget_memo.pdf') as doc:
        text = _extract_plain_page_text(doc[0])
    assert not has_excessive_layout_padding(text)
    normalized = ' '.join(text.split())
    assert 'hiring freeze' in normalized
    assert 'modest 2.5% pay increase' in normalized
    assert len(text) < 6000


def test_normal_aligned_table_is_not_rejected():
    assert not has_excessive_layout_padding('Revenue       1000\nExpenses      900\n' * 100)
