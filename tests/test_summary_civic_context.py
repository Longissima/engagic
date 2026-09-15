from types import SimpleNamespace

import pytest

from analysis.llm.civic_context import civic_context
from pipeline.processor import Processor


@pytest.mark.parametrize('kind,name,body', [
    ('city', 'Storm Lake', 'Airport Commission'),
    ('county', 'Example County', 'Board of Supervisors'),
    ('school_district', 'Example Schools', 'School Board'),
])
def test_context_uses_actual_jurisdiction_and_body(kind, name, body):
    context = civic_context(SimpleNamespace(name=name, state='IA', type=kind),
                            SimpleNamespace(title='Regular Meeting', date='2026-09-14'),
                            SimpleNamespace(name=body))
    assert f'Jurisdiction: {name}, IA' in context
    assert f'Jurisdiction type: {kind.replace("_", " ")}' in context
    assert f'Meeting body: {body}' in context


def test_context_and_attachment_inventory_survive_document_representation():
    item = SimpleNamespace(id='fuel', title='Motion to Adjust Fuel Prices', sequence=1,
                           body_text=None, attachments=[SimpleNamespace(name='Staff Report', url='report')])
    requests, _, failed = Processor.__new__(Processor)._build_batch_requests(
        [item], {'report': {'name': 'Staff Report', 'text': 'Attachments: None. Presentation at meeting.', 'page_count': 1}},
        {'fuel': ['report']}, set(), {}, None, None, civic_context_text='Meeting body: Airport Commission')
    assert not failed
    request = requests[0]
    assert '1 linked attachment(s): Staff Report' in request['text']
    assert 'Meeting body: Airport Commission' in request['documents'][0]['text']
    assert 'additional supporting documents' in request['documents'][0]['text']
