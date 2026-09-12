"""Internal, replayable minutes observations; public facts live elsewhere."""
import hashlib
import json
import uuid

from database.repositories_async.base import BaseRepository
from parsing.rollcall.observations import PARSER_VERSION


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), default=str).encode()).hexdigest()


def run_key(row, text_sha, build, inputs):
    return digest([row['meeting_id'], row['content_sha256'], text_sha, build, inputs])


class MinutesRepository(BaseRepository):
    async def cached_text(self, content_sha, extract_version, extraction_fingerprint):
        return await self._fetchval_text(content_sha, extract_version, extraction_fingerprint)

    async def _fetchval_text(self, content_sha, extract_version, extraction_fingerprint):
        row = await self._fetchrow('''
            SELECT t.text_content FROM minutes_parse_runs r
            JOIN minutes_text_snapshots t USING(text_sha256)
            WHERE r.content_sha256=$1 AND r.extract_version IS NOT DISTINCT FROM $2
              AND r.inputs->>'extraction_fingerprint'=$3 AND r.status='completed'
            ORDER BY r.created_at DESC LIMIT 1
        ''', content_sha, extract_version, extraction_fingerprint)
        return row['text_content'] if row else None

    async def is_current(self, meeting_id, key):
        row = await self._fetchrow('''
            SELECT 1 FROM minutes_publications p JOIN minutes_parse_runs r ON r.id=p.run_id
            WHERE p.meeting_id=$1 AND r.cache_key=$2 AND r.status='completed'
        ''', meeting_id, key)
        return row is not None

    async def save_run(self, conn, row, text, build, inputs, parsed=None, *, status='completed', error=None):
        text_sha = hashlib.sha256(text.encode()).hexdigest() if text is not None else None
        key = run_key(row, text_sha, build, inputs)
        if status == 'completed':
            existing = await conn.fetchval("SELECT id FROM minutes_parse_runs WHERE cache_key=$1 AND status='completed'", key)
            if existing:
                return existing
        if text is not None:
            await conn.execute('''INSERT INTO minutes_text_snapshots(text_sha256,text_content)
                VALUES($1,$2) ON CONFLICT DO NOTHING''', text_sha, text)
        run_id = uuid.uuid4().hex
        summary = {'observations': len(parsed.observations), 'published_motions': sum(o.publication is not None for o in parsed.observations),
                   'withheld_observations': sum(o.publication is None for o in parsed.observations)} if parsed else {}
        stored = await conn.fetchval('''
            INSERT INTO minutes_parse_runs(id,cache_key,meeting_id,content_sha256,text_sha256,
                extract_version,parser_version,parser_build,inputs,status,error,summary)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            ON CONFLICT (cache_key) WHERE status='completed' DO NOTHING RETURNING id
        ''', run_id,key,row['meeting_id'],row['content_sha256'],text_sha,row.get('extract_version'),
            PARSER_VERSION,build,inputs,status,error,summary)
        if not stored:
            return await conn.fetchval("SELECT id FROM minutes_parse_runs WHERE cache_key=$1 AND status='completed'", key)
        if parsed:
            await conn.executemany('''
                INSERT INTO minutes_observations(run_id,ordinal,kind,start_offset,end_offset,
                    raw_text,item_id,evidence,interpretation,checks,publication)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            ''', [(run_id,o.ordinal,o.kind,o.start,o.end,o.raw_text,o.item_id,o.evidence,
                   o.interpretation,o.checks,o.publication) for o in parsed.observations])
        return run_id


def roster_ids(rows, api_member_ids=()):
    from parsing.rollcall.identity import MemberIdentities
    return MemberIdentities(rows, api_member_ids).roster_map()


def compare_api(parsed, api_votes, items, roster):
    """Internal comparison only; API roll calls rarely identify individual motions.

    Compare only a single observed motion on a uniquely identified item. Store
    ambiguities and disagreements rather than asserting that two motions match.
    """
    from parsing.rollcall.identity import MemberIdentities
    identities = MemberIdentities(roster, [v['council_member_id'] for v in api_votes])
    per_matter = {}
    for item in items:
        per_matter.setdefault(item.get('matter_id'), []).append(item['id'])
    per_item = {}
    for obs in parsed.observations:
        if obs.item_id and obs.kind == 'motion' and 'superseded_by_summary' not in obs.interpretation:
            per_item.setdefault(obs.item_id, []).append(obs)
    for observations in per_item.values():
        for obs in observations:
            matter = (obs.interpretation.get('item') or {}).get('matter_id')
            api = [v for v in api_votes if v['matter_id'] == matter and
                   (v.get('item_id') == obs.item_id or (not v.get('item_id') and len(per_matter.get(matter, [])) == 1))]
            if not api:
                comparison = {'status': 'no_api_evidence'}
            elif len(observations) != 1 or len({v['motion_index'] for v in api}) != 1:
                comparison = {'status': 'not_comparable', 'reason': 'motion_identity_ambiguous'}
            else:
                from parsing.rollcall.observations import CATEGORY_TO_DB
                ours = {(identities.resolve(n), CATEGORY_TO_DB[v]) for n, v in obs.interpretation.get('confirmed_members', [])}
                theirs = {(identities.canonical_id(v['council_member_id']),v['vote']) for v in api}
                incomplete = any(c['status']=='withheld' and c['field'] in ('member','members','tally')
                                 and c['reason'] not in ('not_established','unanimity_does_not_identify_motion_participants')
                                 for c in obs.checks)
                if not ours and not obs.interpretation.get('members'):
                    comparison = {'status': 'not_comparable', 'reason': 'minutes_do_not_name_voters'}
                elif not ours or any(n is None for n, _ in ours | theirs):
                    comparison = {'status': 'not_comparable', 'reason': 'unresolved_minutes_members'}
                elif incomplete:
                    comparison = {'status': 'not_comparable', 'reason': 'incomplete_minutes_rollcall',
                                  'minutes_only': sorted(ours - theirs), 'api_only': sorted(theirs - ours)}
                elif {i for i,_ in ours} != {i for i,_ in theirs} and not any(
                    left_id == right_id and left_vote != right_vote
                    for left_id,left_vote in ours for right_id,right_vote in theirs
                ):
                    comparison = {'status':'not_comparable','reason':'rollcall_participants_differ',
                                  'minutes_only':sorted(ours-theirs),'api_only':sorted(theirs-ours)}
                else:
                    comparison = {'status': 'agreement' if ours == theirs else 'disagreement',
                        'basis': 'single_observed_motion_candidate',
                        'identity_matches': [identities.receipt(n) for n, _ in obs.interpretation.get('confirmed_members', [])],
                        'minutes_only': sorted(ours - theirs), 'api_only': sorted(theirs - ours)}
            if comparison['status'] == 'disagreement':
                comparison['difference_kind'] = ('api_reports_only_not_voting'
                    if api and all(v['vote']=='not_voting' for v in api) else
                    'api_differences_only_not_voting' if comparison.get('api_only')
                    and all(v=='not_voting' for _,v in comparison['api_only']) else 'ballot_values_differ')
            obs.interpretation['api_comparison'] = comparison
