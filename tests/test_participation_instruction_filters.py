import pytest

from pipeline.filters.item_filters import get_filter_decision, ITEM_FILTER_VERSION


@pytest.mark.parametrize('title', [
    'To speak on an agenda item, please approach the podium when that agenda item is called, and upon',
    'If your issue is not a topic on the agenda, please approach the podium under the "Hear the Public"',
    'If you require accommodation for this meeting, including but not limited to translation services,',
    'IF YOU REQUIRE ACCOMMODATIONS FOR THIS MEETING, contact the clerk.',
    'To speak on an agenda item,\nplease approach the podium',
])
def test_attendee_instructions_are_procedural(title):
    decision = get_filter_decision(title)
    assert decision is not None
    assert decision.reason == 'procedural'
    assert decision.rule_id.startswith('procedural:')
    assert decision.version == ITEM_FILTER_VERSION


@pytest.mark.parametrize('title', [
    'Approve translation services contract for council meetings',
    'Adopt accessibility accommodation policy for public meetings',
    'Purchase accessible podium and hearing assistance equipment',
    'Discussion of changes to Hear the Public participation rules',
    'Amend policy: If you require accommodation for this meeting, contact staff',
])
def test_substantive_participation_proposals_remain_eligible(title):
    assert get_filter_decision(title) is None


@pytest.mark.asyncio
async def test_processor_filters_instruction_before_llm_work():
    from types import SimpleNamespace
    from pipeline.processor import Processor

    item = SimpleNamespace(id='stormlake-instruction', title='To speak on an agenda item, please approach the podium when called')
    processed, pending, writes = await Processor.__new__(Processor)._filter_processed_items([item])
    assert processed == []
    assert pending == []
    assert writes[0]['item_id'] == item.id
    assert writes[0]['filter_reason'] == 'procedural'
