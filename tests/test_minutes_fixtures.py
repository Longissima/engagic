"""Small, manually reviewed corpus slice. No database, object store or fetches."""
import hashlib
import json
from pathlib import Path

import pytest

from database.repositories_async.minutes import compare_api
from parsing.rollcall import DIALECTS
from parsing.rollcall.engine import parse_meeting

FIXTURES = Path(__file__).parent / 'fixtures/rollcall'


def read_fixture(path):
    data = json.loads(path.read_text())
    text = path.with_suffix('.txt').read_text()
    assert hashlib.sha256(text.encode()).hexdigest() == data['text_sha256']
    parsed = parse_meeting(text, data['items'], [r['name'] for r in data['roster']], DIALECTS.get(path.stem))
    return data, text, parsed


@pytest.mark.parametrize('path', sorted(FIXTURES.glob('*.json')), ids=lambda p:p.stem)
def test_pinned_minutes(path):
    data, text, parsed = read_fixture(path)
    assert parsed.evidence_seen == data['expected_motion_candidates']
    actual = [dict(item_id=p.item['id'],motion_index=p.motion_index,outcome=p.outcome,
                   tally=p.tally,member_count=len(p.member_votes)) for p in parsed.published]
    assert actual == data['expected'], data['review_note']
    for observation in parsed.observations:
        if observation.start >= 0:
            assert observation.raw_text == text[observation.start:observation.end]
        if observation.publication:
            assert observation.item_id
            assert any(c['field']=='alignment' and c['status']=='confirmed' for c in observation.checks)
    # Comparison must not modify either source's publication.
    before = [o.publication for o in parsed.observations]
    compare_api(parsed,data['api_votes'],data['items'],data['roster'])
    assert before == [o.publication for o in parsed.observations]


def test_duplicate_rendering_and_contradictions_remain_auditable():
    _, _, parsed = read_fixture(FIXTURES/'acworthGA.json')
    assert len([o for o in parsed.observations if 'superseded_by_summary' in o.interpretation]) == 3
    _, _, parsed = read_fixture(FIXTURES/'milwaukeeWI.json')
    contradiction = next(o for o in parsed.observations if any(c['reason']=='printed_tally_conflicts_with_categories' for c in o.checks))
    assert contradiction.evidence['sections']
    assert contradiction.publication['outcome'] == 'PASS'
    assert contradiction.publication['tally'] == {}
    assert contradiction.interpretation['confirmed_members'] == []
    assert not any(m['confirmed'] for m in contradiction.interpretation['members'])
    assert any(o.item_id is None and o.evidence.get('outcome') == 'PASS' for o in parsed.observations)


def test_explicit_failure_is_not_overridden_by_majority():
    items = [dict(id='i',title='Budget Amendment',agenda_number='1.',sequence=1,matter_id='m',matter_file=None)]
    parsed = parse_meeting('1. Budget Amendment\nMotion failed 4-2.\n',items,[])
    assert len(parsed.published) == 1
    assert parsed.published[0].outcome == 'FAIL'
    assert parsed.published[0].tally == {'yes':4,'no':2}


def test_mismatching_full_name_is_saved_without_false_identity():
    items = [dict(id='i',title='Budget Amendment',agenda_number='1.',sequence=1,matter_id='m',matter_file=None)]
    parsed = parse_meeting('1. Budget Amendment\nMotion passed.\nAYES: John Smith, Alice Jones\n',items,['Jane Smith','Alice Jones'])
    obs = parsed.observations[0]
    assert 'John Smith' in obs.raw_text
    assert any(m['raw_name']=='John Smith' and not m['confirmed'] for m in obs.interpretation['members'])
    assert parsed.published[0].member_votes == [('Alice Jones','AYE')]
    assert parsed.published[0].tally == {'yes':2}


def test_api_ballots_do_not_invent_outcomes_or_collapse_vote_categories():
    from pipeline.orchestrators.vote_processor import VoteProcessor
    result = VoteProcessor().process_votes([{'vote':'yes'},{'vote':'yes'},{'vote':'recused'},{'vote':'not_voting'}])
    assert result['outcome'] is None
    assert result['tally']['recused'] == result['tally']['not_voting'] == 1
    assert result['tally']['abstain'] == result['tally']['present'] == 0


@pytest.mark.parametrize('newline', ['\n','\r\n'])
def test_receipts_use_exact_original_text_offsets(newline):
    from parsing.rollcall.evidence import find_evidence
    text = newline.join(['İstanbul / Peña','1. Budget Amendment','Motion passed 4-0.',''])
    ev = find_evidence(text)[0]
    assert text[ev.offset:].startswith('Motion passed')
    assert 'Motion passed 4-0.' in text[ev.source_start:ev.source_end]


def test_unique_cleaned_roster_alias_reuses_existing_identity():
    from database.repositories_async.minutes import roster_ids
    mapping, ambiguous = roster_ids([{'id':'existing','name':'Councilmember Alice Jones (By Request)'}])
    assert mapping['Alice Jones'] == 'existing' and not ambiguous
    mapping, ambiguous = roster_ids([{'id':'a','name':'Alice Jones (Ward 1)'},{'id':'b','name':'Alice Jones (Ward 2)'}])
    assert 'Alice Jones' in ambiguous and 'Alice Jones' not in mapping
