"""Motion-grain publication and reader contracts, including PostgreSQL reruns.

Database cases use an isolated database named by ENGAGIC_TEST_DATABASE_URL.
Each case owns a separate schema and never accesses production tables.
"""
import json
import os
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

from database.models import Vote
from database.repositories_async.council_members import CouncilMemberRepository
from database.repositories_async.document_blobs import DocumentBlobRepository
from database.vote_utils import group_motions
from scripts.parse_minutes_votes import Publishable, persist_meeting, locate
from server.routes import votes as routes

DSN = os.environ.get('ENGAGIC_TEST_DATABASE_URL')
SCHEMA = """
CREATE TABLE meetings (id text PRIMARY KEY, date timestamp, title text);
CREATE TABLE city_matters (id text PRIMARY KEY, banana text, title text, matter_file text, matter_type text,
    first_seen timestamp, last_seen timestamp, appearance_count int, status text, updated_at timestamp);
CREATE TABLE items (id text PRIMARY KEY, meeting_id text, matter_id text, sequence int);
CREATE TABLE council_members (id text PRIMARY KEY, vote_count int DEFAULT 0,
    sponsorship_count int DEFAULT 0, last_seen timestamp, updated_at timestamp);
-- recompute_attribution_counts replaces both counters in one statement, so the
-- minutes publisher reads this table too now that it calls that one owner.
CREATE TABLE sponsorships (id bigserial PRIMARY KEY, council_member_id text NOT NULL,
    matter_id text NOT NULL, is_primary boolean DEFAULT false, sponsor_order int,
    created_at timestamp DEFAULT now());
CREATE TABLE votes (id bigserial PRIMARY KEY, council_member_id text NOT NULL REFERENCES council_members,
    matter_id text NOT NULL REFERENCES city_matters, meeting_id text NOT NULL REFERENCES meetings,
    vote text, vote_date timestamp, sequence int, metadata jsonb, created_at timestamp DEFAULT now(),
    UNIQUE(council_member_id, matter_id, meeting_id));
CREATE TABLE matter_appearances (matter_id text, meeting_id text, item_id text,
    appeared_at timestamp, vote_outcome text, vote_tally jsonb,
    UNIQUE(matter_id, meeting_id, item_id));
CREATE TABLE document_blob (content_sha256 text PRIMARY KEY, original_key text,
    text_key text, extract_version text, extract_method text, ocr_pending_pages integer[]);
"""


@pytest_asyncio.fixture
async def database():
    if not DSN:
        pytest.skip('ENGAGIC_TEST_DATABASE_URL must name an isolated database')
    schema = 'motion_test_' + uuid.uuid4().hex
    conn = await asyncpg.connect(DSN)
    pool = None
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}"')
        await conn.execute(SCHEMA)
        migrations = Path(__file__).resolve().parents[1] / 'database/migrations'
        for name in ('040_minutes_documents.sql', '041_votes_motion_grain.sql',
                     '043_votes_motion_grain_phase2.sql', '044_item_motions.sql'):
            await conn.execute((migrations / name).read_text())
        await conn.set_type_codec('jsonb', schema='pg_catalog', encoder=json.dumps, decoder=json.loads)
        await conn.execute("""
            INSERT INTO meetings VALUES ('meeting', '2026-09-01', 'Council');
            INSERT INTO city_matters(id, banana, title) VALUES ('matter', 'testCA', 'Housing');
            INSERT INTO items VALUES ('item', 'meeting', 'matter', 1), ('item2', 'meeting', 'matter', 2);
            INSERT INTO council_members(id) VALUES ('alice'), ('bob');
            INSERT INTO document_blob(content_sha256,original_key,text_key,extract_version,extract_method) VALUES ('sha', 'original/sha', 'text/sha', '2', 'text');
            INSERT INTO minutes_documents(meeting_id,content_sha256,source_identity) VALUES ('meeting','sha','url');
        """)
        async def init(c):
            await c.set_type_codec('jsonb', schema='pg_catalog', encoder=json.dumps, decoder=json.loads)
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2, init=init,
                                        server_settings={'search_path': schema})
        yield conn, pool
    finally:
        if pool:
            await pool.close()
        await conn.execute('SET search_path TO public')
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await conn.close()


ROW = {'meeting_id': 'meeting', 'banana': 'testCA', 'date': datetime(2026, 9, 1), 'content_sha256': 'sha'}
ROSTER = {'Alice': 'alice', 'Bob': 'bob'}


def motion(index=0, **kwargs):
    return replace(Publishable(index, 'item', 'matter', 'named', [('Alice', 'AYE'), ('Bob', 'NO')],
                   'failed', {'yes': 1, 'no': 1}, 'Amend housing rules',
                   {'sha256': 'sha', 'start': 0, 'end': 19, 'unit': 'unicode_codepoint'}), **kwargs)


def published(*motions):
    return {(m.item_id, m.motion_index): m for m in motions}


def test_grouped_rollcalls_never_add_separate_motions():
    rows = [Vote(council_member_id=str(n), matter_id='m', meeting_id='meeting', item_id='i',
                 motion_index=index, vote='yes') for index in (0, 1) for n in range(8)]
    groups = group_motions(rows, [])
    assert [g['tally']['yes'] for g in groups] == [8, 8]
    assert [g['motion_index'] for g in groups] == [0, 1]


def test_receipt_units_are_explicit_with_unicode():
    text = 'Peña voted. Motion passed.'
    receipt = locate(text, 'Motion passed.', 'sha', hint=12)
    assert receipt['unit'] == 'unicode_codepoint'
    assert text[receipt['start']:receipt['end']] == 'Motion passed.'


@pytest.mark.asyncio
async def test_all_motions_persist_and_summary_is_last(database):
    conn, pool = database
    amendment = motion()
    tally_only = motion(1, method='tally', votes=[], tally={'yes': 8, 'no': 0}, outcome='passed')
    outcome_only = motion(2, method='outcome', votes=[], tally={}, outcome='passed')
    assert await persist_meeting(conn, ROW, published(amendment, tally_only, outcome_only), ROSTER)
    repo = CouncilMemberRepository(pool)
    votes = await repo.get_votes_for_meeting('meeting')
    assert len(votes) == 2
    assert votes[0].source == 'minutes' and votes[0].receipt['sha256'] == 'sha'
    groups = group_motions(votes, await repo.get_motions(meeting_id='meeting'))
    assert len(groups) == 3 and groups[1]['tally']['yes'] == 8
    assert groups[2]['tally'] is None and groups[2]['votes'] == []
    assert await repo.get_vote_tally_for_matter('matter') is None
    appearance = await conn.fetchrow('SELECT * FROM matter_appearances')
    assert appearance['vote_outcome'] == 'passed'
    assert appearance['vote_source'] == 'minutes'
    record = await repo.get_member_voting_record('alice')
    assert record[0]['motion_text'] == amendment.motion_text


@pytest.mark.asyncio
async def test_rerun_retracts_members_and_motions_keeps_ids_and_recounts(database):
    conn, pool = database
    await persist_meeting(conn, ROW, published(motion(), motion(1)), ROSTER)
    retained_id = await conn.fetchval("SELECT id FROM votes WHERE council_member_id='alice' AND motion_index=0")
    await persist_meeting(conn, ROW, published(motion(votes=[('Alice', 'NO')], tally={'yes': 0, 'no': 1})), ROSTER)
    rows = await conn.fetch('SELECT * FROM votes')
    assert len(rows) == 1 and rows[0]['id'] == retained_id and rows[0]['vote'] == 'no'
    assert await conn.fetchval('SELECT count(*) FROM item_motions') == 1
    assert dict(await conn.fetch('SELECT id, vote_count FROM council_members')) == {'alice': 1, 'bob': 0}
    assert await persist_meeting(conn, ROW, {}, ROSTER)
    assert await conn.fetchval('SELECT count(*) FROM votes') == 0
    assert await conn.fetchval('SELECT count(*) FROM item_motions') == 0
    assert await conn.fetchval('SELECT vote_outcome FROM matter_appearances') is None
    assert await conn.fetchval('SELECT sum(vote_count) FROM council_members') == 0


@pytest.mark.asyncio
async def test_same_matter_two_items_do_not_collide(database):
    conn, pool = database
    await persist_meeting(conn, ROW, published(motion(), motion(item_id='item2')), ROSTER)
    assert await conn.fetchval('SELECT count(*) FROM votes') == 4
    assert await conn.fetchval('SELECT count(*) FROM item_motions') == 2
    assert await conn.fetchval('SELECT count(*) FROM matter_appearances') == 2
    assert len(await CouncilMemberRepository(pool).get_votes_for_matter('matter')) == 4


@pytest.mark.asyncio
async def test_revisions_and_api_rows_protect_against_stale_publication(database):
    conn, pool = database
    await persist_meeting(conn, ROW, published(motion()), ROSTER)
    assert not await persist_meeting(conn, {**ROW, 'content_sha256': 'old'}, {}, ROSTER)
    repo = CouncilMemberRepository(pool)
    assert await repo.record_vote('alice', 'matter', 'meeting', 'yes', conn=conn)
    assert await persist_meeting(conn, ROW, {}, ROSTER)
    assert await conn.fetchval("SELECT count(*) FROM votes WHERE source='minutes'") == 0
    assert await conn.fetchval("SELECT count(*) FROM votes WHERE source='api'") == 1
    # Repeated API corrections keep the legacy NULL item grain and counter.
    assert not await repo.record_vote('alice', 'matter', 'meeting', 'no', conn=conn)
    assert await conn.fetchval("SELECT vote FROM votes WHERE source='api'") == 'no'
    assert await conn.fetchval("SELECT vote_count FROM council_members WHERE id='alice'") == 1


@pytest.mark.asyncio
async def test_failed_publication_rolls_back_retractions(database):
    conn, _ = database
    await persist_meeting(conn, ROW, published(motion()), ROSTER)
    with pytest.raises(ValueError, match='Unmapped'):
        await persist_meeting(conn, ROW, published(motion(votes=[('Missing', 'AYE')])), ROSTER)
    assert await conn.fetchval('SELECT count(*) FROM votes') == 2
    assert await conn.fetchval('SELECT vote_outcome FROM matter_appearances') == 'failed'


@pytest.mark.asyncio
async def test_meeting_api_includes_tally_only_and_keeps_outcomes_separate(database, monkeypatch):
    conn, pool = database
    await persist_meeting(conn, ROW, published(motion(), motion(1, votes=[], method='tally',
        tally={'yes': 8, 'no': 0}, outcome='passed')), ROSTER)
    async def require(*_):
        return SimpleNamespace(title='Council', date=ROW['date'])
    async def matters(_):
        return {'matter': SimpleNamespace(title='Housing', matter_file='R-1')}
    monkeypatch.setattr(routes, 'require_meeting', require)
    db = SimpleNamespace(council_members=CouncilMemberRepository(pool), matters=SimpleNamespace(get_matters_batch=matters))
    response = await routes.get_meeting_votes('meeting', db)
    result = response['matters_with_votes'][0]
    assert response['motion_count'] == 2 and response['total'] == 2
    assert result['tally'] == {'yes': 8, 'no': 0}
    assert [m['outcome'] for m in result['motions']] == ['failed', 'passed']


@pytest.mark.asyncio
async def test_minutes_discovery_preserves_revision_and_readiness(database):
    conn, pool = database
    docs = await DocumentBlobRepository(pool).get_minutes_documents('meeting')
    assert len(docs) == 1 and docs[0]['text_ready']
    assert docs[0]['content_sha256'] == 'sha' and docs[0]['source_identity'] == 'url'


@pytest.mark.asyncio
async def test_item_deletion_preserves_evidence_and_motion_identity(database):
    conn, pool = database
    await persist_meeting(conn, ROW, published(motion(), motion(item_id='item2')), ROSTER)
    repo = CouncilMemberRepository(pool)
    await repo.record_vote('alice', 'matter', 'meeting', 'yes', conn=conn)
    await conn.execute("DELETE FROM items WHERE id IN ('item', 'item2')")
    assert await conn.fetchval('SELECT count(*) FROM item_motions') == 2
    assert await conn.fetchval('SELECT count(*) FROM votes') == 5
    assert await conn.fetchval("SELECT vote_count FROM council_members WHERE id='alice'") == 3
    groups = await repo.get_motion_groups(meeting_id='meeting')
    assert len(groups) == 2
    assert {m['item_id'] for m in groups} == {'item', 'item2'}
    assert len(await repo.get_motion_groups(meeting_id='meeting', include_api_comparison=True)) == 3


@pytest.mark.asyncio
async def test_provenance_only_correction_updates_receipt(database):
    conn, pool = database
    repo = CouncilMemberRepository(pool)
    async with conn.transaction():
        await repo.record_vote('alice', 'matter', 'meeting', 'yes', conn=conn,
            item_id='item', source='minutes', receipt={'start': 1})
        await repo.record_vote('alice', 'matter', 'meeting', 'yes', conn=conn,
            item_id='item', source='minutes', receipt={'start': 10})
    assert await conn.fetchval('SELECT receipt FROM votes') == {'start': 10}
    assert await conn.fetchval("SELECT vote_count FROM council_members WHERE id='alice'") == 1


@pytest.mark.asyncio
async def test_changed_item_link_rejects_inflight_parse(database):
    conn, _ = database
    await conn.execute("INSERT INTO city_matters(id) VALUES ('new-matter')")
    await conn.execute("UPDATE items SET matter_id='new-matter' WHERE id='item'")
    assert not await persist_meeting(conn, ROW, published(motion()), ROSTER)
    assert await conn.fetchval('SELECT count(*) FROM votes') == 0


@pytest.mark.asyncio
async def test_matter_api_exposes_a_motion_without_any_individual_votes(database, monkeypatch):
    conn, pool = database
    await conn.execute("""
        CREATE TABLE committees(id text PRIMARY KEY, name text);
        ALTER TABLE matter_appearances ADD COLUMN committee text;
        ALTER TABLE matter_appearances ADD COLUMN committee_id text;
        ALTER TABLE matter_appearances ADD COLUMN sequence int;
    """)
    await persist_meeting(conn, ROW, published(motion(votes=[], method='outcome', tally={}, outcome='passed')), ROSTER)
    async def require(*_):
        return SimpleNamespace(title='Housing')
    async def outcomes(_):
        return []
    monkeypatch.setattr(routes, 'require_matter', require)
    db = SimpleNamespace(pool=pool, council_members=CouncilMemberRepository(pool),
        matters=SimpleNamespace(get_matter_vote_outcomes=outcomes))
    response = await routes.get_matter_votes('matter', db)
    assert response['votes'] == [] and response['tally'] is None
    assert response['outcome'] == 'passed'
    assert len(response['votes_by_meeting']) == 1
    assert response['votes_by_meeting'][0]['motions'][0]['method'] == 'outcome'


@pytest.mark.asyncio
async def test_relink_copy_preserves_detached_item_keys_and_counts(database):
    from scripts.relink_vendor_keyed_items import COPY_VOTES_SQL
    conn, _ = database
    await persist_meeting(conn, ROW, published(motion(), motion(item_id='item2')), ROSTER)
    await conn.execute("DELETE FROM items")
    await conn.execute("INSERT INTO city_matters(id) VALUES ('new-matter')")
    await conn.execute(COPY_VOTES_SQL, 'new-matter', ['matter'])
    rows = await conn.fetch("SELECT item_key FROM votes WHERE matter_id='new-matter'")
    assert len(rows) == 4 and {r['item_key'] for r in rows} == {'item', 'item2'}
    await conn.execute("DELETE FROM votes WHERE matter_id='matter'")
    assert await conn.fetchval("SELECT vote_count FROM council_members WHERE id='alice'") == 2


@pytest.mark.asyncio
async def test_rollback_refuses_to_discard_motion_evidence(database):
    conn, _ = database
    await persist_meeting(conn, ROW, published(motion()), ROSTER)
    down = Path(__file__).resolve().parents[1] / 'database/migrations/044_item_motions.down.sql'
    with pytest.raises(asyncpg.RaiseError, match='contains evidence'):
        async with conn.transaction():
            await conn.execute(down.read_text())
    assert await conn.fetchval('SELECT count(*) FROM item_motions') == 1


@pytest.mark.asyncio
async def test_upgrade_repairs_existing_counts_and_marks_minutes_ownership(database):
    conn, _ = database
    migrations = Path(__file__).resolve().parents[1] / 'database/migrations'
    await conn.execute((migrations / '044_item_motions.down.sql').read_text())
    await conn.execute("""
        INSERT INTO votes(council_member_id, matter_id, meeting_id, item_id, vote, source)
        VALUES ('alice','matter','meeting','item','yes','minutes');
        UPDATE council_members SET vote_count=99;
        INSERT INTO matter_appearances(matter_id,meeting_id,item_id,vote_outcome,vote_tally)
        VALUES ('matter','meeting','item','passed','{"yes":1,"method":"named"}');
    """)
    await conn.execute((migrations / '044_item_motions.sql').read_text())
    assert dict(await conn.fetch('SELECT id,vote_count FROM council_members')) == {'alice': 1, 'bob': 0}
    assert await conn.fetchval('SELECT vote_source FROM matter_appearances') == 'minutes'
    assert await conn.fetchval('SELECT item_key FROM votes') == 'item'


@pytest.mark.asyncio
async def test_minutes_discovery_does_not_substitute_an_older_ready_revision(database):
    conn, pool = database
    await conn.execute("""
        INSERT INTO document_blob(content_sha256,text_key) VALUES ('new-sha','text/new-sha');
        INSERT INTO minutes_documents(meeting_id,content_sha256,source_identity,ingested_at)
        VALUES ('meeting','new-sha','url',now()+interval '1 day');
    """)
    docs = await DocumentBlobRepository(pool).get_minutes_documents('meeting')
    assert docs[0]['content_sha256'] == 'new-sha' and docs[0]['text_ready'] is False
    assert docs[1]['content_sha256'] == 'sha' and docs[1]['text_ready'] is True


@pytest.mark.asyncio
async def test_source_rows_do_not_collide_at_same_item_motion(database):
    conn, pool = database
    repo = CouncilMemberRepository(pool)
    await repo.record_vote('alice','matter','meeting','yes',item_id='item',conn=conn)
    await persist_meeting(conn,ROW,published(motion()),ROSTER)
    groups = await repo.get_motion_groups(meeting_id='meeting')
    assert [g['source'] for g in groups] == ['minutes']
    assert groups[0]['selection_basis'] == 'minutes'
    comparison = await repo.get_motion_groups(meeting_id='meeting', include_api_comparison=True)
    assert {g['source'] for g in comparison} == {'api','minutes'}
    assert len(comparison) == 2
    history = await repo.get_member_voting_record('alice')
    assert history and all(v['source'] == 'minutes' for v in history)
    await persist_meeting(conn,ROW,{},ROSTER)
    assert await conn.fetchval('SELECT source FROM votes') == 'api'
    assert (await repo.get_motion_groups(meeting_id='meeting'))[0]['selection_basis'] == 'api_fallback_no_confirmed_minutes'


@pytest.mark.asyncio
async def test_audit_history_cache_lineage_and_failure_preserve_publication(database, monkeypatch):
    from collections import Counter
    from database.repositories_async.minutes import MinutesRepository
    from scripts import parse_minutes_votes as writer
    conn, pool = database
    await conn.execute('''
        ALTER TABLE items ADD COLUMN title text;
        ALTER TABLE items ADD COLUMN matter_file text;
        ALTER TABLE items ADD COLUMN agenda_number text;
        UPDATE items SET title='Budget Amendment',agenda_number='1.' WHERE id='item';
        ALTER TABLE council_members ADD COLUMN banana text;
        ALTER TABLE council_members ADD COLUMN name text;
        UPDATE council_members SET banana='testCA',name=CASE id WHEN 'alice' THEN 'Alice Jones' ELSE 'Bob Brown' END;
    ''')
    class Corpus:
        calls = 0
        async def lookup_extraction(self, _):
            self.calls += 1
            return {'text': '1. Budget Amendment\nMotion passed.\nAYES: Alice Jones, Nobody Unknown\nNAYS: Bob Brown\n'}
    corpus = Corpus()
    db = SimpleNamespace(pool=pool,council_members=CouncilMemberRepository(pool))
    audit = MinutesRepository(pool)
    row = {**ROW,'extract_version':'2','text_extracted_at':'2026-09-01','text_key':'text/sha','extract_method':'text'}
    counts, reasons = Counter(), Counter()
    await writer.process_one(db,corpus,audit,row,'build-one',True,counts,reasons)
    assert counts['meetings_written'] == 1 and not counts['failed']
    run_id = await conn.fetchval('SELECT run_id FROM minutes_publications')
    obs = await conn.fetchrow('SELECT * FROM minutes_observations WHERE run_id=$1',run_id)
    assert 'Nobody Unknown' in obs['raw_text']
    assert any(c['reason']=='unresolved_name' for c in obs['checks'])
    assert await conn.fetchval('SELECT count(*) FROM votes') == 2
    votes = await db.council_members.get_votes_for_meeting('meeting')
    assert all(v.parse_run_id == run_id and v.observation_ordinal == obs['ordinal'] for v in votes)
    await writer.process_one(db,corpus,audit,row,'build-one',True,counts,reasons)
    assert counts['unchanged_runs'] == 1 and corpus.calls == 1
    # A parser update appends an immutable run and moves only the current pointer.
    await writer.process_one(db,corpus,audit,row,'build-two',True,counts,reasons)
    assert await conn.fetchval('SELECT count(*) FROM minutes_parse_runs') == 2
    new_id = await conn.fetchval('SELECT run_id FROM minutes_publications')
    assert new_id != run_id
    assert await conn.fetchval('SELECT raw_text FROM minutes_observations WHERE run_id=$1',run_id) == obs['raw_text']
    # Missing source and a thrown parser error both retain the last good facts.
    async def missing(_):
        return None
    monkeypatch.setattr(corpus,'lookup_extraction',missing)
    await writer.process_one(db,corpus,audit,{**row,'text_extracted_at':None},'build-two',True,counts,reasons)
    assert counts['missing_text'] == 1
    def broken(*_):
        raise ValueError('simulated parser failure')
    monkeypatch.setattr(writer,'parse_meeting',broken)
    await writer.process_one(db,corpus,audit,row,'build-three',True,counts,reasons)
    assert counts['failed'] == 1
    assert await conn.fetchval('SELECT run_id FROM minutes_publications') == new_id
    assert await conn.fetchval('SELECT count(*) FROM votes') == 2
    assert {r['status'] for r in await conn.fetch('SELECT status FROM minutes_parse_runs')} == {'completed','failed','missing_text'}


@pytest.mark.asyncio
async def test_withheld_final_motion_does_not_publish_earlier_disposition(database):
    conn, _ = database
    await persist_meeting(conn,ROW,published(motion()),ROSTER,final_indices={'item':1})
    assert await conn.fetchval('SELECT count(*) FROM item_motions') == 1
    assert await conn.fetchval('SELECT count(*) FROM matter_appearances') == 0


@pytest.mark.asyncio
async def test_cleared_item_link_or_changed_title_rejects_inflight_parse(database):
    from scripts.parse_minutes_votes import ITEMS_SQL
    conn, _ = database
    await conn.execute('ALTER TABLE items ADD COLUMN title text; ALTER TABLE items ADD COLUMN matter_file text; ALTER TABLE items ADD COLUMN agenda_number text')
    expected = [dict(r) for r in await conn.fetch(ITEMS_SQL,'meeting')]
    await conn.execute("UPDATE items SET matter_id=NULL WHERE id='item'")
    assert not await persist_meeting(conn,ROW,published(motion()),ROSTER,expected_items=expected)
    await conn.execute("UPDATE items SET matter_id='matter',title='Different agenda subject' WHERE id='item'")
    assert not await persist_meeting(conn,ROW,published(motion()),ROSTER,expected_items=expected)
    assert await conn.fetchval('SELECT count(*) FROM item_motions') == 0


@pytest.mark.asyncio
async def test_identity_republication_moves_minutes_votes_without_changing_api(database):
    from collections import Counter
    from database.repositories_async.minutes import MinutesRepository
    from scripts.parse_minutes_votes import process_one
    conn,pool=database
    await conn.execute('''
        ALTER TABLE items ADD COLUMN title text;
        ALTER TABLE items ADD COLUMN matter_file text;
        ALTER TABLE items ADD COLUMN agenda_number text;
        UPDATE items SET title='Budget Amendment',agenda_number='1.' WHERE id='item';
        ALTER TABLE council_members ADD COLUMN name text;
        ALTER TABLE council_members ADD COLUMN banana text;
        UPDATE council_members SET banana='testCA',name=CASE id WHEN 'alice' THEN 'Alice Jones' ELSE 'Councilmember Alice Jones' END;
    ''')
    repo=CouncilMemberRepository(pool)
    await repo.record_vote('alice','matter','meeting','yes',item_id='item',conn=conn)
    original_api=dict(await conn.fetchrow("SELECT * FROM votes WHERE source='api'"))
    await persist_meeting(conn,ROW,published(motion(votes=[('Bob','AYE')],outcome='passed',tally={'yes':1})),ROSTER)
    class Corpus:
        async def lookup_extraction(self,_):
            return {'text':'1. Budget Amendment\nMotion passed.\nAYES: Alice Jones\n'}
    counts=Counter()
    await process_one(SimpleNamespace(pool=pool,council_members=repo),Corpus(),MinutesRepository(pool),
                      {**ROW,'extract_version':'2'},'identity-test',True,counts,Counter())
    assert counts['meetings_written']==1 and not counts['failed']
    assert dict(await conn.fetchrow("SELECT * FROM votes WHERE source='api'")) == original_api
    assert await conn.fetchval("SELECT council_member_id FROM votes WHERE source='minutes'")=='alice'
    # Alice cast one vote, recorded twice. The retained API row is audit
    # provenance, not a second ballot, so the counter reports one.
    assert dict(await conn.fetch('SELECT id,vote_count FROM council_members'))=={'alice':1,'bob':0}
    observation=await conn.fetchrow('SELECT interpretation FROM minutes_observations')
    assert observation['interpretation']['member_identity_matches'][0]['member_id']=='alice'


@pytest.mark.asyncio
async def test_corpus_readiness_uses_current_revision_and_exposes_older_text(database):
    from scripts.backfill_corpus_attachments import READY_IDENTITIES_SQL
    from scripts.ingest_minutes import SOURCE_STATE_SQL
    from server.routes.meetings import get_meeting_minutes

    conn, pool = database
    # Exercise the additive migration, including a repeat application.
    await conn.execute('ALTER TABLE document_blob DROP COLUMN ocr_pending_pages')
    migration = Path('database/migrations/045_corpus_pending_pages.sql').read_text()
    await conn.execute(migration)
    await conn.execute(migration)
    await conn.execute("""
        CREATE TABLE document_source (
            source_identity text, content_sha256 text, first_seen timestamp,
            last_seen timestamp, last_validated_at timestamp, last_observed_at timestamp
        );
        INSERT INTO document_blob(content_sha256) VALUES ('new-sha');
        INSERT INTO document_source VALUES
            ('url', 'sha', '2026-01-01', '2026-09-11', '2026-01-01', '2026-09-12'),
            ('url', 'new-sha', '2026-02-01', '2026-02-01', '2026-02-01', '2026-02-01');
        INSERT INTO minutes_documents(meeting_id,content_sha256,source_identity,ingested_at)
            VALUES ('meeting','new-sha','url', CURRENT_TIMESTAMP + interval '1 minute');
        UPDATE document_blob SET extract_method='pymupdf-partial', ocr_pending_pages=ARRAY[3]
            WHERE content_sha256='sha';
    """)
    assert await conn.fetch(READY_IDENTITIES_SQL, ['url'], ['1', '2']) == []
    state = await conn.fetchrow(SOURCE_STATE_SQL, ['url'], ['1', '2'], 7)
    assert state['content_sha256'] == 'new-sha'
    assert state['corpus_ready'] is False
    repo = DocumentBlobRepository(pool)
    docs = await repo.get_minutes_documents('meeting')
    assert docs[0]['text_ready'] is False
    assert docs[1]['text_ready'] is True
    assert docs[1]['text_incomplete'] is True
    assert docs[1]['ocr_pending_pages'] == [3]

    class Meetings:
        async def get_meeting(self, meeting_id):
            return {'id': meeting_id}

    db = SimpleNamespace(document_blobs=repo, meetings=Meetings())
    response = await get_meeting_minutes('meeting', db)
    assert response['current_content_sha256'] == 'new-sha'
    assert response['current_text_ready'] is False
    assert response['older_text_available'] is True
    assert response['older_text_content_sha256'] == 'sha'
    assert response['fallback_used'] is False
    # A genuine origin validation can make previously seen bytes current again.
    await conn.execute("UPDATE document_source SET last_validated_at='2026-09-12' WHERE content_sha256='sha'")
    assert len(await conn.fetch(READY_IDENTITIES_SQL, ['url'], ['1', '2'])) == 1


@pytest.mark.asyncio
async def test_minutes_own_appearance_outcome_over_api(database):
    from database.repositories_async.matters import MatterRepository
    conn, pool = database
    await conn.execute("""INSERT INTO matter_appearances(matter_id,meeting_id,item_id,vote_outcome,vote_source)
        VALUES ('matter','meeting','item','failed','api')""")
    await persist_meeting(conn, ROW, published(motion(outcome="passed")), ROSTER)
    row = await conn.fetchrow('SELECT vote_outcome,vote_source FROM matter_appearances')
    assert dict(row) == {'vote_outcome':'passed', 'vote_source':'minutes'}
    await MatterRepository(pool).update_appearance_outcome('matter','meeting','item','failed',{'yes':0,'no':2},conn=conn)
    assert dict(await conn.fetchrow('SELECT vote_outcome,vote_source FROM matter_appearances')) == dict(row)


@pytest.mark.asyncio
async def test_ballot_preference_reaches_api_votes_with_no_item_identity(database):
    """The production shape: an API vote carries item_key='', never NULL.

    votes.item_key is NOT NULL DEFAULT '' (migration 044), so a preference
    written against IS NULL is unreachable and reads both sources silently.
    Every API vote in production has no item identity, so this is the only
    shape that matters and the one no earlier case covered.
    """
    conn, pool = database
    repo = CouncilMemberRepository(pool)
    await repo.record_vote('alice', 'matter', 'meeting', 'yes', conn=conn)
    assert await conn.fetchval("SELECT item_key FROM votes WHERE source='api'") == ''
    await persist_meeting(conn, ROW, published(motion()), ROSTER)

    history = await repo.get_member_voting_record('alice')
    assert [v['source'] for v in history] == ['minutes']
    assert await conn.fetchval("SELECT vote_count FROM council_members WHERE id='alice'") == 1
    # The topic profile is the third reader of the same preference: Alice voted
    # once on housing, not twice, and the minutes ballot is the one that counts.
    await conn.execute("""
        CREATE TABLE matter_topics (matter_id text, topic text);
        CREATE TABLE item_topics (item_id text, topic text);
        INSERT INTO matter_topics VALUES ('matter', 'housing');
    """)
    assert await repo.get_member_topic_profile('alice') == [
        {'topic': 'housing', 'yes': 1, 'no': 0, 'abstain': 0, 'absent': 0,
         'other': 0, 'total': 1, 'yes_rate': 1.0}]


@pytest.mark.asyncio
async def test_minutes_that_name_nobody_do_not_delete_an_api_ballot(database):
    """Deduplication only removes a duplicate.

    A tally- or outcome-only minutes motion records an outcome and names no
    voter, so it competes with nothing at ballot grain. Yielding to it would
    erase the only record that a member voted at all. Outcome grain still
    prefers minutes; that is asserted separately on matter_appearances.
    """
    conn, pool = database
    repo = CouncilMemberRepository(pool)
    await repo.record_vote('alice', 'matter', 'meeting', 'yes', conn=conn)
    await persist_meeting(conn, ROW, published(
        motion(method='outcome', votes=[], tally={}, outcome='passed')), ROSTER)

    history = await repo.get_member_voting_record('alice')
    assert [v['source'] for v in history] == ['api']
    assert await conn.fetchval("SELECT vote_count FROM council_members WHERE id='alice'") == 1
    assert await conn.fetchval('SELECT vote_source FROM matter_appearances') == 'minutes'
