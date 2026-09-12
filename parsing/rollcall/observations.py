"""Capture minutes evidence first; validate individual claims for publication.

Nothing here fetches documents or writes a database. The returned observations
include unaligned, unresolved and procedural evidence. Only ``published`` is a
public projection. All offsets refer to the exact supplied Unicode text.
"""
from collections import Counter
from dataclasses import asdict, dataclass, field
import hashlib
from pathlib import Path
import re

from parsing.rollcall.align import anchor_items, blocks
from parsing.rollcall.attendance import parse_attendance
from parsing.rollcall.evidence import Evidence, Section, find_evidence
from parsing.rollcall.names import clean_name, fold
from parsing.rollcall.spike import load_spike_parser, norm_file


PARSER_VERSION = "minutes-observations-1"
VOTE_LANGUAGE = re.compile(r"\b(?:motion|moved|seconded|ayes?|nays?|yeas?|noes|abstain\w*|recus\w*|unanimous\w*|roll\s*call)\b", re.I)
CATEGORY_TO_DB = {"AYE": "yes", "NO": "no", "ABSTAIN": "abstain", "RECUSED": "recused",
                  "ABSENT": "absent", "EXCUSED": "absent", "PRESENT": "present", "NONVOTING": "not_voting"}


def parser_build() -> str:
    root = Path(__file__).resolve().parents[2]
    paths = sorted((root / 'parsing/rollcall').glob('*.py')) + [
        root / 'scripts/spikes/rollcall/parse.py', root / 'scripts/parse_minutes_votes.py',
        root / 'database/repositories_async/minutes.py', root / 'database/id_generation.py']
    return hashlib.sha256(b''.join(str(p.relative_to(root)).encode() + p.read_bytes() for p in paths)).hexdigest()


@dataclass
class Observation:
    ordinal: int
    start: int
    end: int
    raw_text: str
    kind: str
    evidence: dict = field(default_factory=dict)
    item_id: str | None = None
    interpretation: dict = field(default_factory=dict)
    checks: list[dict] = field(default_factory=list)
    publication: dict | None = None


def _check(obs, field, status, reason, **details):
    obs.checks.append(dict(field=field, status=status, reason=reason, **details))


def _driver_evidence(text, items, dialect):
    """Reuse template extraction, but apply the same claim gates as generic text."""
    parse = load_spike_parser()
    by_file = {}
    for item in items:
        key = norm_file(dialect, item.get('matter_file'))
        if key:
            by_file.setdefault(key, []).append(item)
    cursor = 0
    out = []
    for passage in parse.PARSERS[dialect](text):
        if not passage.sections:
            continue
        probe = re.compile(r'\s+'.join(re.escape(w) for w in passage.motion_text.split()))
        match = probe.search(text, cursor)
        if match:
            start = match.start()
            cursor = match.end()
        else:
            # A template normalizes page furniture. Keep its result even if
            # an exact receipt cannot be recovered, but never publish it.
            start = -1
        candidates = by_file.get(norm_file(dialect, passage.matter_file), [])
        item = candidates[0] if len(candidates) == 1 else None
        ev = Evidence(passage.motion_text, passage.outcome, start,
            sections=[Section(value, names, stated) for value, names, stated in passage.sections])
        out.append((ev, item, 'driver:' + dialect, match.end() if match else -1))
    # Include the complete category lists, bounded by the next template motion
    # or file anchor. The raw source also lives in the run's text snapshot.
    for index, (ev, item, rung, end) in enumerate(out):
        if ev.offset < 0:
            yield ev, item, rung, -1, -1
            continue
        next_start = next((entry[0].offset for entry in out[index + 1:] if entry[0].offset > ev.offset), len(text))
        stop = min(next_start, end + 2500)
        yield ev, item, rung, ev.offset, stop


def _validate(obs, ev, item, rung, motion_index, gazetteer, known_names, attendance):
    from parsing.rollcall.engine import ItemVotes, _is_heading, _membership_ok
    obs.item_id = item.get('id') if item else None
    obs.interpretation = {'item': item, 'alignment': rung, 'motion_index': motion_index,
                          'raw_outcome': ev.outcome, 'subject_disposition': ev.disposition,
                          'reported_body': ev.reported_body, 'unanimous': ev.unanimous, 'members': []}
    if not item:
        _check(obs, 'alignment', 'withheld', 'no_unique_item')
    elif ev.reported_body and not ev.sections and not ev.movers:
        # A bare mention with no roll call and nobody moving is prose about another
        # body, not a recorded action: "per the recommendation of the X Committee".
        _check(obs, 'alignment', 'withheld', 'reported_committee_action')
    elif ev.procedural or _is_heading(str(item.get('title') or '')):
        _check(obs, 'alignment', 'withheld', 'procedural_or_heading')
    elif (attendance.present and not attendance.partial and gazetteer.canonical
            and not _membership_ok(ev.movers, gazetteer, attendance.present)):
        _check(obs, 'alignment', 'withheld', 'mover_outside_recorded_body')
    else:
        _check(obs, 'alignment', 'confirmed', rung)
    aligned = obs.checks[-1]['status'] == 'confirmed'
    if obs.start < 0:
        _check(obs, 'receipt', 'withheld', 'source_span_unlocated')
        aligned = False
    else:
        _check(obs, 'receipt', 'confirmed', 'exact_text_span')
    # Preserve stated outcomes, including outcomes which contradict a simple
    # majority. Quorum, supermajority and charter rules are not inferred here.
    outcome = ev.outcome
    _check(obs, 'outcome', 'confirmed' if outcome else 'withheld',
           'explicit_result' if outcome else 'not_stated')
    section_tally = {}
    structural = True
    raw_seen = Counter(fold(clean_name(name)) for s in ev.sections for name in s.names)
    resolved = []
    for section in ev.sections:
        key = CATEGORY_TO_DB[section.value]
        n = section.stated if section.stated is not None else len(section.names)
        section_tally[key] = section_tally.get(key, 0) + n
        section_ok = section.stated is None or not section.names or len(section.names) == section.stated
        if not section_ok:
            structural = False
            _check(obs, 'members', 'withheld', 'section_count_mismatch', category=section.value,
                   stated=section.stated, extracted=len(section.names))
        for name in section.names:
            canonical = gazetteer.resolve(name)
            # A surname-only attendance entry does not create a new person.
            trusted = canonical in known_names if canonical else False
            duplicate = raw_seen[fold(clean_name(name))] > 1
            reason = 'duplicate_name' if duplicate else 'unresolved_name' if not trusted else 'resolved_name'
            accepted = trusted and not duplicate and section_ok
            obs.interpretation['members'].append(dict(raw_name=name, category=section.value,
                canonical_name=canonical if trusted else None, confirmed=accepted, reason=reason))
            _check(obs, 'member', 'confirmed' if accepted else 'withheld', reason, raw_name=name,
                   canonical_name=canonical if trusted else None)
            if accepted:
                resolved.append((canonical, section.value))
    # Two spellings which collapse to the same person cannot cast two votes.
    duplicate_people = {name for name, count in Counter(n for n, _ in resolved).items() if count > 1}
    if duplicate_people:
        _check(obs, 'members', 'withheld', 'duplicate_resolved_member', names=sorted(duplicate_people))
        resolved = [(n, v) for n, v in resolved if n not in duplicate_people]
    tally = {}
    tally_basis = None
    if ev.tally:
        tally = {'yes': ev.tally[0], 'no': ev.tally[1]}
        if ev.tally[2]:
            tally['other'] = ev.tally[2]  # an unlabeled third column is not necessarily abstention
        tally_basis = 'printed_tally'
    elif ev.sections and structural and not any(n > 1 for n in raw_seen.values()) and not duplicate_people:
        tally = section_tally
        tally_basis = 'printed_categories' if all(s.stated is not None for s in ev.sections) else 'counted_names'
    if tally and not any(tally.values()):
        # Nobody voted for anything: that is an outcome, not a recorded tally.
        _check(obs, 'tally', 'withheld', 'tally_is_all_zero')
        tally, tally_basis = {}, None
    if ev.tally and ev.sections and (
        section_tally.get('yes', 0), section_tally.get('no', 0)
    ) != ev.tally[:2]:
        _check(obs, 'tally', 'withheld', 'printed_tally_conflicts_with_categories')
        tally = {}
        resolved = []
    else:
        if tally and ev.sections and structural:
            tally.update({key:value for key,value in section_tally.items() if key not in ('yes','no')})
        _check(obs, 'tally', 'confirmed' if tally else 'withheld', tally_basis or 'not_established')
    if ev.unanimous and not ev.named:
        _check(obs, 'members', 'withheld', 'unanimity_does_not_identify_motion_participants')
    for member in obs.interpretation['members']:
        if member['confirmed'] and (member['canonical_name'], member['category']) not in resolved:
            member['confirmed'] = False
            member['reason'] = 'withheld_by_motion_consistency_check'
    obs.interpretation['confirmed_members'] = resolved
    obs.interpretation['tally_basis'] = tally_basis if tally else None
    if not aligned or not (outcome or tally or resolved):
        return None
    method = 'named' if resolved else 'tally' if tally else 'outcome'
    pub = ItemVotes(item=item, motion_index=motion_index, method=method, outcome=outcome,
        tally=tally, member_votes=resolved, motion_text=ev.result_text, offset=obs.start,
        rung=rung, observation_index=obs.ordinal, tally_basis=tally_basis if tally else None,
        reported_body=ev.reported_body)
    obs.publication = asdict(pub)
    return pub


def observe_meeting(text, items, roster, dialect=None):
    from parsing.rollcall.engine import Gazetteer, MeetingParse, Abstention
    attendance = parse_attendance(text)
    result = MeetingParse(attendance=attendance, items_total=len(items))
    anchors = anchor_items(text, items)
    item_blocks = blocks(text, anchors)
    result.items_anchored = len(anchors)
    known_names = set(roster) | {clean_name(n) for n in attendance.present + attendance.absent
                               if len(fold(clean_name(n)).split()) >= 2}
    gazetteer = Gazetteer(sorted(known_names))
    # Gazetteer chooses one canonical spelling for folded aliases.
    known_names = set(gazetteer.canonical)
    result.roster_source = 'roster' if roster else attendance.source
    candidates = []
    if dialect:
        candidates.extend(_driver_evidence(text, items, dialect))
    driver_ranges = [(start, end) for _, _, _, start, end in candidates if start >= 0]
    # Discover across the whole document, including material with no item
    # anchors. Re-read an aligned block to prevent lists crossing its boundary.
    local = {}
    for block in item_blocks:
        for ev in find_evidence(text[block['start']:block['end']]):
            absolute = block['start'] + ev.offset
            local[absolute] = (ev, block)
    for ev in find_evidence(text):
        if any(start <= ev.offset < end for start, end in driver_ranges):
            continue
        pair = local.get(ev.offset)
        if pair:
            ev, block = pair
            start = block['start'] + ev.source_start
            end = block['start'] + ev.source_end
            candidates.append((ev, block['item'], block['rung'], start, end))
        else:
            candidates.append((ev, None, 'unaligned', ev.source_start, ev.source_end))
    candidates.sort(key=lambda c: c[3])
    motion_counts = Counter()
    prior_by_item = {}
    for ev, item, rung, start, end in candidates:
        ordinal = len(result.observations)
        obs = Observation(ordinal, start, end, text[start:end] if start >= 0 else '',
                          'motion', asdict(ev))
        prior = prior_by_item.get(item['id']) if item else None
        summary = False
        if prior and (ev.outcome == prior[0].outcome or ev.disposition == 'denied'):
            previous, previous_obs, previous_index = prior
            labeled = bool(re.match(r'RESULT\s*:', ev.result_text, re.I))
            bare = bool(re.fullmatch(r'(?:Adopted|Approved|Passed)\.?', ev.result_text, re.I))
            # These are repeated renderings of a single result, not another
            # motion. Keep both observations and link the stronger summary.
            distance = ev.offset - previous.offset
            section_yes = sum(s.stated if s.stated is not None else len(s.names)
                              for s in ev.sections if s.value == 'AYE')
            section_no = sum(s.stated if s.stated is not None else len(s.names)
                             for s in ev.sections if s.value == 'NO')
            summary = (0 < distance < 700 and labeled and previous.tally is not None
                       and previous.tally[:2] == (section_yes, section_no)) or (0 < distance < 160 and bare)
            if ev.disposition == 'denied':
                summary = summary or (0 < distance < 350 and labeled and previous.unanimous
                                      and section_yes > 0 and section_no == 0)
                if summary:
                    ev.outcome = previous.outcome
                    if ev.tally is None:
                        ev.tally = previous.tally
        if summary:
            # The receipt must include the narrative that establishes the
            # motion outcome as well as its repeated result box.
            obs.start = min(obs.start, prior[1].start)
            obs.end = max(obs.end, prior[1].end)
            obs.raw_text = text[obs.start:obs.end]
            index = prior[2]
            prior[1].interpretation['superseded_by_summary'] = ordinal
            prior[1].publication = None
            result.published = [p for p in result.published if p.observation_index != prior[1].ordinal]
            _check(prior[1], 'publication', 'withheld', 'duplicate_rendering', summary_ordinal=ordinal)
        else:
            index = motion_counts[item['id']] if item else 0
            if item:
                motion_counts[item['id']] += 1
        pub = _validate(obs, ev, item, rung, index, gazetteer, known_names, attendance)
        if summary:
            obs.interpretation['summary_of'] = prior[1].ordinal
        if item:
            prior_by_item[item['id']] = (ev, obs, index)
        result.observations.append(obs)
        if pub:
            result.published.append(pub)
        else:
            reasons = [c['reason'] for c in obs.checks if c['status'] == 'withheld']
            result.abstained.append(Abstention(item=item, reasons=reasons, motion_text=ev.result_text))
            if 'procedural_or_heading' in reasons:
                result.procedural_skipped += 1
    result.evidence_seen = len(candidates)
    # Retain likely vote language that none of the format parsers understands.
    # It supplies a compact debugging bucket, never a publication shortcut.
    covered = sorted((o.start, o.end) for o in result.observations if o.start >= 0)
    for match in VOTE_LANGUAGE.finditer(text):
        if any(start <= match.start() < end for start, end in covered):
            continue
        start = text.rfind('\n', 0, match.start()) + 1
        end = text.find('\n', match.end())
        end = len(text) if end < 0 else end
        obs = Observation(len(result.observations), start, end, text[start:end], 'unparsed_vote_language')
        _check(obs, 'extraction', 'withheld', 'unsupported_or_nonvote_language')
        result.observations.append(obs)
        covered.append((start, end))
    return result
