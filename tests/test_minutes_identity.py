from types import SimpleNamespace

from database.repositories_async.minutes import compare_api
from parsing.rollcall.identity import MemberIdentities


def test_title_alias_prefers_unique_api_identity_even_when_exact_alias_exists():
    resolver = MemberIdentities([{'id':'api','name':'ALD. BROWER','has_api_votes':True},
                                 {'id':'minutes','name':'BROWER'}])
    assert resolver.resolve('Brower') == resolver.canonical_id('minutes') == 'api'
    assert resolver.receipt('BROWER')['matched_existing_ids'] == ['api','minutes']


def test_compound_surname_requires_unique_api_surname():
    rows = [{'id':'api','name':'Diana Romero Campbell','has_api_votes':True},
            {'id':'short','name':'Romero Campbell'}]
    resolver = MemberIdentities(rows)
    assert resolver.resolve('Romero Campbell') == 'api'
    rows.append({'id':'other','name':'John Campbell','has_api_votes':True})
    assert MemberIdentities(rows).resolve('Romero Campbell') == 'short'
    assert resolver.resolve('John Campbell') is None


def test_conflicting_full_first_names_are_never_reconciled():
    resolver = MemberIdentities([{'id':'a','name':'Jane Smith','has_api_votes':True},
                                 {'id':'b','name':'John Smith'}])
    assert resolver.resolve('John Smith') == 'b'
    assert resolver.resolve('Jane Smith') == 'a'


def test_two_api_identities_with_same_normalized_name_stay_ambiguous():
    resolver = MemberIdentities([{'id':'a','name':'ALD. BROWER','has_api_votes':True},
                                 {'id':'b','name':'Brower','has_api_votes':True}])
    assert resolver.resolve('Brower') is None
    assert resolver.canonical_id('a') is None


def comparison(rows, members, api_votes, checks=()):
    item = {'id':'i','matter_id':'m'}
    obs = SimpleNamespace(item_id='i',kind='motion',checks=list(checks),
        interpretation={'item':item,'confirmed_members':members})
    parsed = SimpleNamespace(observations=[obs])
    api = [dict(council_member_id=i,vote=v,matter_id='m',item_id='i',motion_index=0) for i,v in api_votes]
    compare_api(parsed,api,[item],rows)
    return obs.interpretation['api_comparison']


def test_comparison_matches_alias_ids_without_changing_ballots():
    rows = [{'id':'a','name':'ALD. BROWER'},{'id':'b','name':'BROWER'}]
    result = comparison(rows,[('BROWER','AYE')],[('a','yes')])
    assert result['status']=='agreement'
    result = comparison(rows,[('BROWER','AYE')],[('a','no')])
    assert result['status']=='disagreement'
    assert result['minutes_only']==[('a','yes')]
    assert result['api_only']==[('a','no')]


def test_incomplete_minutes_are_not_scored_as_a_ballot_disagreement():
    result = comparison([{'id':'a','name':'Alice Jones'},{'id':'b','name':'Bob Smith'}],
        [('Alice Jones','AYE')],[('a','yes'),('b','no')],
        [{'field':'member','status':'withheld','reason':'unresolved_name'}])
    assert result['status']=='not_comparable'
    assert result['reason']=='incomplete_minutes_rollcall'


def test_unknown_label_cannot_turn_remote_participants_into_absent_voters():
    from parsing.rollcall.evidence import find_evidence
    text = 'RESULT: APPROVED\nAYES: DeSart, Yoo\nABSENT: Cronin Cahill, Schwarze\nREMOTE: Galassi, LaPlante\n'
    evidence = find_evidence(text)[0]
    absent = next(s for s in evidence.sections if s.value=='ABSENT')
    assert absent.names == ['Cronin Cahill','Schwarze']
    assert all('LaPlante' not in s.names for s in evidence.sections)


def test_vice_mayor_title_and_not_present_category_are_explicit():
    from parsing.rollcall.names import clean_name
    from parsing.rollcall.evidence import find_evidence
    assert clean_name('Vice Mayor Ben Sorensen') == 'Ben Sorensen'
    evidence = find_evidence('APPROVED\nYea: 1 - Vice Mayor Sorensen\nNot Present: 1 - Mayor Trantalis\n')[0]
    assert [(s.value,s.names) for s in evidence.sections] == [('AYE',['Sorensen']),('ABSENT',['Trantalis'])]


def test_matching_partial_ballots_do_not_imply_api_disagreement():
    result = comparison([{'id':'a','name':'Alice Jones'},{'id':'b','name':'Bob Smith'}],
        [('Alice Jones','AYE')],[('a','yes'),('b','absent')])
    assert result['status']=='not_comparable'
    assert result['reason']=='rollcall_participants_differ'


def test_pinned_remote_and_not_present_passages():
    import json
    from pathlib import Path
    from parsing.rollcall.evidence import find_evidence
    cases={c['city']:c for c in json.loads((Path(__file__).parent/'fixtures/minutes_identity_passages.json').read_text())}
    remote=find_evidence(cases['dupagecountyIL']['source']['raw_text'])[0]
    assert next(s.names for s in remote.sections if s.value=='ABSENT')==['Cronin Cahill','Schwarze']
    assert not any('LaPlante' in s.names or 'Galassi' in s.names for s in remote.sections)
    absent=find_evidence(cases['fortlauderdaleFL']['source']['raw_text'])[0]
    assert next(s.names for s in absent.sections if s.value=='ABSENT')==['Trantalis']


def test_middle_initial_can_be_omitted_but_cannot_conflict():
    rows=[{'id':'api','name':'Klarissa J. Peña','has_api_votes':True},
          {'id':'short','name':'Klarissa Pena'},{'id':'different','name':'Klarissa B. Peña'}]
    resolver=MemberIdentities(rows)
    assert resolver.resolve('Klarissa Pena')=='api'
    assert resolver.resolve('Klarissa B. Peña')=='different'


def test_two_aliases_cannot_publish_two_ballots_for_same_existing_person():
    from parsing.rollcall.engine import parse_meeting
    from parsing.rollcall.identity import reconcile_members
    items=[dict(id='i',title='Budget Amendment',agenda_number='1.',sequence=1,matter_id='m',matter_file=None)]
    rows=[{'id':'api','name':'Diana Romero Campbell','has_api_votes':True},
          {'id':'short','name':'Romero Campbell'}]
    parsed=parse_meeting('1. Budget Amendment\nMotion passed.\nAYES: Diana Romero Campbell, Romero Campbell\n',items,[r['name'] for r in rows])
    reconcile_members(parsed,MemberIdentities(rows))
    assert parsed.published[0].outcome=='PASS'
    assert parsed.published[0].member_votes==[]
    assert parsed.published[0].tally=={}


def test_raw_middle_initial_conflict_is_withheld_after_gazetteer_matching():
    from parsing.rollcall.engine import parse_meeting
    from parsing.rollcall.identity import reconcile_members
    items=[dict(id='i',title='Budget Amendment',agenda_number='1.',sequence=1,matter_id='m',matter_file=None)]
    rows=[{'id':'api','name':'Klarissa J. Peña','has_api_votes':True}]
    parsed=parse_meeting('1. Budget Amendment\nMotion passed.\nAYES: Klarissa B. Peña\n',items,[r['name'] for r in rows])
    reconcile_members(parsed,MemberIdentities(rows))
    assert parsed.published[0].member_votes==[]
    assert parsed.published[0].outcome=='PASS'
    assert 'Klarissa B. Peña' in parsed.observations[0].raw_text
