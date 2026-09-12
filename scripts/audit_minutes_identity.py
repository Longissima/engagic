"""Read-only identity/comparison audit over stored observations; no document fetches."""
import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
from types import SimpleNamespace

import asyncpg
from config import config
from database.repositories_async.minutes import compare_api
from scripts.parse_minutes_votes import ROSTER_SQL


async def audit(path):
    conn = await asyncpg.connect(config.get_postgres_dsn())
    await conn.set_type_codec('jsonb',schema='pg_catalog',encoder=json.dumps,decoder=json.loads)
    before,after,transitions=Counter(),Counter(),Counter()
    samples,rosters={},{}
    try:
        async with conn.transaction(readonly=True):
            async for row in conn.cursor('''SELECT r.id,r.meeting_id,r.inputs,m.banana,
                (SELECT jsonb_agg(to_jsonb(o) ORDER BY ordinal) FROM minutes_observations o WHERE o.run_id=r.id) observations
                FROM minutes_publications p JOIN minutes_parse_runs r ON r.id=p.run_id
                JOIN meetings m ON m.id=p.meeting_id
                WHERE EXISTS(SELECT 1 FROM minutes_observations o WHERE o.run_id=r.id
                    AND o.interpretation->'api_comparison'->>'status' IN ('agreement','disagreement','not_comparable'))
                ORDER BY m.banana,r.meeting_id''', prefetch=10):
                city = row['banana']
                if city not in rosters:
                    rosters[city] = [dict(r) for r in await conn.fetch(ROSTER_SQL,city)]
                observations = [SimpleNamespace(**o) for o in row['observations']]
                old = {o.ordinal:o.interpretation.get('api_comparison',{}).get('status') for o in observations}
                parsed=SimpleNamespace(observations=observations)
                compare_api(parsed,row['inputs']['api_votes'],row['inputs']['items'],rosters[city])
                for obs in observations:
                    previous=old[obs.ordinal]
                    if previous not in ('agreement','disagreement','not_comparable'):
                        continue
                    result=obs.interpretation['api_comparison']
                    status=result['status']
                    before[previous]+=1
                    after[status]+=1
                    transitions[previous+' -> '+status]+=1
                    kind='remaining_disagreement' if status=='disagreement' else 'identity_match' if previous=='disagreement' and status=='agreement' else None
                    if kind and (kind,city) not in samples:
                        samples[kind,city]={'kind':kind,'city':city,'meeting_id':row['meeting_id'],
                            'ordinal':obs.ordinal,'raw_text':obs.raw_text,'comparison':result,
                            'member_names':{r['id']:r['name'] for r in rosters[city] if r['id'] in
                                {i for key in ('minutes_only','api_only') for i,v in result.get(key,[])}}}
        result={'before':dict(before),'after':dict(after),'transitions':dict(transitions),'samples':list(samples.values())}
        Path(path).write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
        print(json.dumps({k:v for k,v in result.items() if k!='samples'},indent=2))
        print('Saved audit:',path)
    finally:
        await conn.close()


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output',default='/tmp/engagic-minutes-identity-audit.json')
    asyncio.run(audit(ap.parse_args().output))
