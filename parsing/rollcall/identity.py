"""Deterministic bridges between existing member IDs; no entity rows are merged."""
import re
from collections import defaultdict

from parsing.rollcall.names import clean_name, fold


def name_key(name):
    return ' '.join(re.findall(r"[^\W_]+", fold(clean_name(name)), re.UNICODE))


class MemberIdentities:
    def __init__(self, rows, api_member_ids=()):
        self.rows = {r['id']: r for r in rows}
        api_ids = set(api_member_ids) | {r['id'] for r in rows if r.get('has_api_votes')}
        groups = defaultdict(list)
        for row in rows:
            key = name_key(row['name'])
            if key:
                groups[key].append(row['id'])
        self.keys = {}
        self.id_map = {}
        self.reasons = {}
        self.ambiguous = set()
        for key, ids in groups.items():
            anchors = set(ids) & api_ids
            target = next(iter(anchors)) if len(anchors) == 1 else ids[0] if len(ids) == 1 else None
            if target is None:
                self.ambiguous.add(key)
                continue
            self.keys[key] = target
            for member_id in ids:
                self.id_map[member_id] = target
                self.reasons[member_id] = 'normalized_name' if member_id != target else 'existing_identity'
        # A minutes-only shortened surname can reuse a unique API roster entry.
        # Require an exact token suffix and a unique API surname, not fuzzy
        # first-name matching. Multiple API identities always remain ambiguous.
        api_by_last = defaultdict(set)
        for member_id in api_ids:
            if member_id in self.rows:
                tokens = name_key(self.rows[member_id]['name']).split()
                if tokens:
                    api_by_last[tokens[-1]].add(member_id)
        for key, ids in groups.items():
            if set(ids) & api_ids or key in self.ambiguous:
                continue
            short = key.split()
            anchors = api_by_last[short[-1]]
            if len(anchors) != 1:
                continue
            target = next(iter(anchors))
            full = name_key(self.rows[target]['name']).split()
            suffix = len(full) > len(short) and full[-len(short):] == short
            middle_variant = (len(full) >= 2 and len(short) >= 2 and full[0] == short[0]
                and full[-1] == short[-1] and (len(full)==2 or len(short)==2 or
                    (len(full)==len(short) and all(a==b or (len(a)==1 and b.startswith(a))
                     or (len(b)==1 and a.startswith(b)) for a,b in zip(full[1:-1],short[1:-1])))))
            if suffix or middle_variant:
                self.keys[key] = target
                for member_id in ids:
                    self.id_map[member_id] = target
                    self.reasons[member_id] = 'unique_api_name_suffix' if suffix else 'unique_api_middle_name_variant'

    def resolve(self, name):
        return self.keys.get(name_key(name))

    def canonical_id(self, member_id):
        return self.id_map.get(member_id)

    def roster_map(self):
        mapping, ambiguous = {}, set()
        for row in self.rows.values():
            for name in (row['name'], clean_name(row['name'])):
                resolved = self.resolve(name)
                if resolved:
                    mapping[name] = resolved
                else:
                    ambiguous.add(name)
        return mapping, ambiguous

    def raw_name_conflicts(self, raw_name, canonical_name):
        """Check the original spelling too, before trusting a gazetteer alias."""
        raw_id, target = self.resolve(raw_name), self.resolve(canonical_name)
        if raw_id and target and raw_id != target:
            return True
        if not target:
            return False
        raw = name_key(raw_name).split()
        full = name_key(self.rows[target]['name']).split()
        if len(raw) >= 3 and len(full) >= 3 and raw[0] == full[0] and raw[-1] == full[-1]:
            return len(raw) != len(full) or any(
                a != b and not (len(a)==1 and b.startswith(a)) and not (len(b)==1 and a.startswith(b))
                for a,b in zip(raw[1:-1],full[1:-1]))
        return False

    def receipt(self, name):
        target = self.resolve(name)
        aliases = [r['id'] for r in self.rows.values() if name_key(r['name']) == name_key(name)]
        return {'name': name, 'member_id': target, 'matched_existing_ids': sorted(aliases),
                'basis': sorted({self.reasons.get(i, 'ambiguous_identity') for i in aliases})}


def reconcile_members(parsed, identities):
    """Resolve entity IDs before comparison/publication, retaining withheld claims."""
    from collections import Counter
    from dataclasses import asdict
    retained = []
    for pub in parsed.published:
        obs = parsed.observations[pub.observation_index]
        matches = [identities.receipt(n) for n,_ in pub.member_votes]
        obs.interpretation['member_identity_matches'] = matches
        counts = Counter(m['member_id'] for m in matches if m['member_id'])
        ambiguous_keys = identities.ambiguous
        blocked = {n for n,_ in pub.member_votes if name_key(n) in ambiguous_keys or
                   (identities.resolve(n) and counts[identities.resolve(n)] > 1)}
        blocked.update(m['canonical_name'] for m in obs.interpretation['members']
                       if m['confirmed'] and identities.raw_name_conflicts(m['raw_name'],m['canonical_name']))
        if blocked:
            pub.member_votes = [(n,v) for n,v in pub.member_votes if n not in blocked]
            obs.interpretation['confirmed_members'] = pub.member_votes
            obs.checks.append(dict(field='members',status='withheld',reason='ambiguous_or_duplicate_member_identity',names=sorted(blocked)))
            for member in obs.interpretation['members']:
                if member['canonical_name'] in blocked:
                    member.update(confirmed=False,reason='ambiguous_or_duplicate_member_identity')
            if pub.tally_basis == 'counted_names':
                pub.tally = {}
                pub.tally_basis = None
                obs.checks.append(dict(field='tally',status='withheld',reason='ambiguous_or_duplicate_member_identity'))
            pub.method = 'named' if pub.member_votes else 'tally' if pub.tally else 'outcome'
        if pub.member_votes or pub.tally or pub.outcome:
            obs.publication = asdict(pub)
            retained.append(pub)
        else:
            obs.publication = None
    parsed.published = retained
