"""Small saved-minutes regression bucket for motion semantics and list formats."""
import hashlib
import json
from pathlib import Path

from parsing.rollcall.engine import parse_meeting, Gazetteer
from parsing.rollcall.evidence import find_evidence
from database.vote_utils import group_motions


def test_reviewed_minutes_passages():
    cases = json.loads(Path('tests/fixtures/minutes_review_passages.json').read_text())
    for case in cases:
        text = case['text']
        assert hashlib.sha256(text.encode()).hexdigest() == case['text_sha256']
        evidence = find_evidence(text)
        city = case['city']
        if city in ('wauwatosaWI', 'winderGA'):
            assert any(e.outcome == 'PASS' for e in evidence)
            denied = next(e for e in evidence if e.disposition == 'denied')
            assert denied.outcome is None
        elif city == 'winterparkFL':
            assert evidence == []  # Historical permit denial, not a motion.
        elif city == 'wausauWI':
            motion = next(e for e in evidence if e.outcome == 'FAIL')
            assert [(s.value, len(s.names)) for s in motion.sections] == [('AYE', 7), ('NO', 1), ('ABSTAIN', 0)]
        elif city == 'contracostacountyCA':
            names = next(s.names for e in evidence for s in e.sections if s.value == 'AYE')
            assert names == ['Carlson', 'Scales-Preston', 'Burgis', 'Gioia', 'Andersen']
            assert Gazetteer(['John Gioia']).resolve(names[3]) == 'John Gioia'
        elif city == 'fresnocountyCA':
            recused = next(s for e in evidence for s in e.sections if s.value == 'RECUSED')
            assert recused.names == ['Pacheco'] and recused.stated == 1
        elif city == 'cincinnatiOH':
            names = next(s.names for e in evidence for s in e.sections if s.value == 'AYE')
            assert len(names) == 9 and 'Johnson' in names


def test_successful_motion_to_deny_remains_passed():
    text = '''1. Appeal
Motion to deny the appeal. Motion carried 4-0.
RESULT: DENIED
Ayes: Alice Jones, Bob Smith, Carol White, Dan Black
'''
    roster = ['Alice Jones', 'Bob Smith', 'Carol White', 'Dan Black']
    parsed = parse_meeting(text, [dict(id='i', title='Appeal', agenda_number='1.', sequence=1)], roster)
    assert len(parsed.published) == 1
    assert parsed.published[0].outcome == 'PASS'
    assert parsed.published[0].tally == {'yes': 4, 'no': 0}
    assert parsed.observations[-1].interpretation['subject_disposition'] == 'denied'
    assert parsed.observations[-1].interpretation['summary_of'] == 0
    assert 'Motion carried 4-0.' in parsed.observations[-1].raw_text
    # A disposition without a motion result does not infer pass/fail from the tally.
    evidence = find_evidence('RESULT: DENIED\nAyes: 4\nNoes: 0')[0]
    assert evidence.outcome is None


def test_compact_counts_and_dates_do_not_become_empty_or_fabricated_tallies():
    ev = find_evidence('Yes 7, No 1, Abstained 0\nMOTION FAILED.')[0]
    assert ev.outcome == 'FAIL'
    assert [(s.value, s.stated) for s in ev.sections] == [('AYE', 7), ('NO', 1), ('ABSTAIN', 0)]
    ev = find_evidence('The motion passed. Meeting held 5-21-26.')[0]
    assert ev.tally is None


def test_minutes_primary_does_not_fill_missing_voters_from_api():
    base = dict(meeting_id='meeting', matter_id='matter', item_id='item', motion_index=0)
    api = [{**base, 'source': 'api', 'council_member_id': 'alice', 'vote': 'yes'}]
    minutes = [{**base, 'source': 'minutes', 'outcome': 'passed', 'tally': {}, 'method': 'outcome'}]
    groups = group_motions(api, minutes, prefer_minutes=True)
    assert len(groups) == 1 and groups[0]['votes'] == []
    assert groups[0]['selection_basis'] == 'minutes'
    fallback = group_motions(api, [], prefer_minutes=True)
    assert fallback[0]['selection_basis'] == 'api_fallback_no_confirmed_minutes'
    assert len(group_motions(api, minutes)) == 2  # Internal source comparison.


def test_inline_empty_categories_are_not_people():
    ev = find_evidence('Motion Passed.\nYes-Alice Jones, Bob Smith, No-None, Abstain-None.')[0]
    assert [(s.value, s.names, s.stated) for s in ev.sections] == [
        ('AYE', ['Alice Jones', 'Bob Smith'], None), ('NO', [], 0), ('ABSTAIN', [], 0)]


def test_reported_committee_approval_does_not_become_council_motion():
    text = '''1. Housing resolution
The resolution was approved by the Budget and Finance
Committee.
Motion carried 5-0.
'''
    parsed = parse_meeting(text, [dict(id='i', title='Housing resolution', agenda_number='1.', sequence=1)], [])
    assert len(parsed.published) == 1
    assert parsed.published[0].tally == {'yes': 5, 'no': 0}
    assert any(c['reason']=='reported_committee_action' for o in parsed.observations for c in o.checks)
