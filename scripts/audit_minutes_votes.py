#!/usr/bin/env python3
"""Inspect internal minutes runs and replay exact saved text/inputs, without fetching.

    python -m scripts.audit_minutes_votes --meeting-id MEETING --replay
    python -m scripts.audit_minutes_votes --run-id RUN --details

Read-only. Withheld evidence is intentionally available here, not in public APIs.
For the pinned offline corpus slice: pytest tests/test_minutes_fixtures.py -q
"""
import argparse
import asyncio
from dataclasses import asdict
import json

from database.db_postgres import Database
from database.repositories_async.minutes import compare_api
from parsing.rollcall.engine import parse_meeting
from parsing.rollcall.identity import MemberIdentities, reconcile_members
from parsing.rollcall.observations import parser_build


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument('--meeting-id')
    target.add_argument('--run-id')
    ap.add_argument('--replay', action='store_true')
    ap.add_argument('--details', action='store_true', help='include internal raw evidence and checks')
    ap.add_argument('--reason', help='only show observations containing this withheld reason')
    args = ap.parse_args()
    db = await Database.create()
    try:
        async with db.pool.acquire() as conn:
            run = await conn.fetchrow('''SELECT r.*, t.text_content,
                    EXISTS(SELECT 1 FROM minutes_publications p WHERE p.run_id=r.id) AS currently_published
                FROM minutes_parse_runs r LEFT JOIN minutes_text_snapshots t USING(text_sha256)
                WHERE ($1::text IS NULL OR r.meeting_id=$1) AND ($2::text IS NULL OR r.id=$2)
                ORDER BY r.created_at DESC LIMIT 1''',args.meeting_id,args.run_id)
            if run is None:
                raise SystemExit('No saved minutes run for this identifier')
            observations = [dict(r) for r in await conn.fetch('SELECT * FROM minutes_observations WHERE run_id=$1 ORDER BY ordinal',run['id'])]
        result = {k:v for k,v in dict(run).items() if k not in ('inputs','text_content')}
        if args.replay:
            if run['text_content'] is None:
                raise SystemExit('This attempt has no saved text to replay')
            inputs = run['inputs']
            parsed = parse_meeting(run['text_content'],inputs['items'],[r['name'] for r in inputs['roster']],inputs['dialect'])
            reconcile_members(parsed,MemberIdentities(inputs['roster'],[v['council_member_id'] for v in inputs['api_votes']]))
            compare_api(parsed,inputs['api_votes'],inputs['items'],inputs['roster'])
            result['replay'] = {'parser_build':parser_build(),'same_build':parser_build()==run['parser_build'],
                'observations':len(parsed.observations),'confirmed_motion_candidates':len(parsed.published),
                'motions':[asdict(p) for p in parsed.published]}
        if args.reason:
            observations = [o for o in observations if any(c['reason']==args.reason for c in o['checks'])]
        result['observations'] = observations if args.details else [dict(ordinal=o['ordinal'],kind=o['kind'],
            item_id=o['item_id'],published=o['publication'] is not None,
            withheld=[c['reason'] for c in o['checks'] if c['status']=='withheld']) for o in observations]
        print(json.dumps(result,indent=2,ensure_ascii=False,default=str))
    finally:
        await db.close()


if __name__ == '__main__':
    asyncio.run(main())
