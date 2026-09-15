from copy import deepcopy

from vendors.adapters.civicplus_adapter_async import AsyncCivicPlusAdapter


def meeting(title, vendor_id, date='2026-09-08T00:00:00'):
    return dict(title=title, vendor_id=vendor_id, start=date,
                body_name='City Commission', packet_url=f'https://example.gov/{vendor_id}')


def test_aventura_item_documents_attach_to_their_meetings():
    adapter = AsyncCivicPlusAdapter('fl-aventura')
    regular = meeting('City Commission Regular Meeting', '516')
    budget = meeting('City Commission First Budget Public Hearing Agenda', '513')
    exhibit = meeting('City Commission Meeting (Item 10 – Resolution Exhibit A Final Assessment Roll)', '517')
    budget_doc = meeting('First Budget Public Hearing (Item 3B – Tentative Operating and Capital Budget for Fiscal Year 2026/2027)', '514')
    other_date = meeting('City Commission Second Budget Public Hearing', '519', '2026-09-15T00:00:00')
    result = adapter._dedupe_by_date([exhibit, budget_doc, regular, budget, other_date])
    assert [m['vendor_id'] for m in result] == ['516', '513', '519']
    assert regular['agenda_sources'][0]['url'] == exhibit['packet_url']
    assert budget['agenda_sources'][0]['url'] == budget_doc['packet_url']
    assert regular['packet_url'].endswith('/516')
    assert adapter._dedupe_by_date(result + [exhibit]) == result


def test_ambiguous_or_wrong_date_supplement_is_preserved():
    adapter = AsyncCivicPlusAdapter('fl-aventura')
    exhibit = meeting('City Commission Meeting (Item 10 – Assessment Roll)', '517')
    parent = meeting('City Commission Regular Meeting', '516')
    wrong_date = meeting('City Commission Regular Meeting', '999', '2026-09-09T00:00:00')
    assert len(adapter._dedupe_by_date([deepcopy(exhibit), wrong_date])) == 2
    second = meeting('City Commission Meeting', '998')
    assert len(adapter._dedupe_by_date([exhibit, parent, second])) == 3
