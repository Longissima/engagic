"""Shared vote tally and outcome computation logic."""

from typing import Dict, List

# Canonical vote value mappings
VOTE_MAP = {
    "yes": "yes",
    "aye": "yes",
    "yea": "yes",
    "no": "no",
    "nay": "no",
    "abstain": "abstain",
    "abstained": "abstain",
    "absent": "absent",
    "excused": "absent",
    "not present": "absent",
    "present": "present",
    "recused": "recused",
    "not_voting": "not_voting",
}


def compute_vote_tally(votes: List[Dict]) -> Dict[str, int]:
    """Compute vote tally from raw vote data."""
    tally = {"yes": 0, "no": 0, "abstain": 0, "absent": 0, "present": 0}

    for vote in votes:
        vote_value = vote.get("vote", "").lower().strip()
        normalized = VOTE_MAP.get(vote_value, "present")
        tally[normalized] = tally.get(normalized, 0) + 1

    return tally


def group_motions(votes, motions, *, prefer_minutes=False):
    """Join individual votes to motions without inventing tally-only voters.

    Legacy API votes have no motion record and are grouped at their stored
    item/motion identity. Results are ordered by meeting date and agenda order;
    summary fields always describe the last recorded motion, never a sum.
    """
    groups = {}
    def key(row):
        return (row["meeting_id"], row["matter_id"], row.get("item_key") or row.get("item_id"), row["motion_index"], row.get("source", "api"))
    for motion in motions:
        row = dict(motion)
        for field in ("vote_date", "updated_at"):
            if row.get(field) is not None and hasattr(row[field], "isoformat"):
                row[field] = row[field].isoformat()
        row["item_key"] = row["item_id"]
        row["votes"] = []
        groups[key(row)] = row
    for vote in votes:
        row = vote.to_dict() if hasattr(vote, "to_dict") else dict(vote)
        k = key(row)
        if k not in groups:
            groups[k] = {field: row.get(field) for field in (
                "meeting_id", "matter_id", "item_id", "item_key", "motion_index", "motion_text",
                "source", "content_sha256", "receipt", "vote_date")}
            groups[k].update(votes=[], tally=None, outcome=None, method=None)
        groups[k]["votes"].append(row)
    recorded = {key(m) for m in motions}
    for k, motion in groups.items():
        motion["votes"].sort(key=lambda v: (
            v.get("sequence") if v.get("sequence") is not None else float("inf"),
            v["council_member_id"], v.get("id") or 0,
        ))
        if k not in recorded:
            motion["tally"] = compute_vote_tally(motion["votes"])
            motion["outcome"] = None  # Individual API votes do not encode a recorded outcome.
    selected = list(groups.values())
    if prefer_minutes:
        minutes_items = {(m["meeting_id"], m["matter_id"], m.get("item_key") or m.get("item_id"))
                         for m in selected if m.get("source") == "minutes"}
        minutes_matters = {(meeting, matter) for meeting, matter, _ in minutes_items}
        selected = [m for m in selected if m.get("source") == "minutes" or not (
            (m["meeting_id"], m["matter_id"], m.get("item_key") or m.get("item_id")) in minutes_items
            or (not (m.get("item_key") or m.get("item_id"))
                and (m["meeting_id"], m["matter_id"]) in minutes_matters))]
        for motion in selected:
            motion["selection_basis"] = ("minutes" if motion.get("source") == "minutes"
                                         else "api_fallback_no_confirmed_minutes")
    return sorted(selected, key=lambda m: (
        m.get("vote_date") or "", m["meeting_id"],
        m.get("item_sequence") if m.get("item_sequence") is not None else -1,
        m.get("item_id") or "", m["motion_index"], m["matter_id"], m.get("source") == "minutes",
    ))
