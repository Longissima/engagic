"""Vote API routes - handles vote records, tallies, and council member voting history."""

from server.utils.validation import capped_limit
from fastapi import APIRouter, Depends

from database.db_postgres import Database
from database.vote_utils import compute_vote_tally
from server.dependencies import get_db
from server.metrics import metrics
from server.utils.validation import (
    require_city,
    require_council_member,
    require_matter,
    require_meeting,
)

router = APIRouter(prefix="/api")


@router.get("/matters/{matter_id}/votes")
async def get_matter_votes(matter_id: str, db: Database = Depends(get_db)):
    """Motion records and votes; summary tally is the last recorded motion."""
    matter = await require_matter(db, matter_id)
    motions = await db.council_members.get_motion_groups(matter_id=matter_id)
    votes = [vote for motion in motions for vote in motion["votes"]]
    outcomes = await db.matters.get_matter_vote_outcomes(matter_id)
    by_meeting = {}
    for motion in motions:
        mid = motion["meeting_id"]
        group = by_meeting.setdefault(mid, {"meeting_id": mid, "votes": [], "motions": []})
        group["votes"].extend(motion["votes"])
        group["motions"].append(motion)
        group.update(computed_tally=motion["tally"], vote_tally=motion["tally"],
                     vote_outcome=motion["outcome"])
    if by_meeting:
        async with db.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT m.id, m.title, m.date,
                    ma.committee, ma.committee_id, c.name AS committee_name
                FROM meetings m
                LEFT JOIN LATERAL (
                    SELECT committee, committee_id FROM matter_appearances
                    WHERE meeting_id = m.id AND matter_id = $1
                    ORDER BY sequence DESC NULLS LAST, item_id DESC LIMIT 1
                ) ma ON true
                LEFT JOIN committees c ON c.id = ma.committee_id
                WHERE m.id = ANY($2::text[])
            """, matter_id, list(by_meeting))
        for row in rows:
            by_meeting[row["id"]].update(
                meeting_title=row["title"], meeting_date=row["date"].isoformat() if row["date"] else None,
                committee=row["committee_name"] or row["committee"], committee_id=row["committee_id"])
    metrics.matter_engagement.labels(action='votes').inc()
    return {"success": True, "matter_id": matter_id, "matter_title": matter.title,
            "votes": votes, "motions": motions,
            "votes_by_meeting": list(reversed(list(by_meeting.values()))),
            "tally": motions[-1]["tally"] if motions else None,
            "outcome": motions[-1]["outcome"] if motions else None,
            "summary_scope": "last_recorded_motion", "outcomes": outcomes}


@router.get("/meetings/{meeting_id}/votes")
async def get_meeting_votes(meeting_id: str, db: Database = Depends(get_db)):
    """Votes grouped by matter, with separate outcomes and tallies per motion."""
    meeting = await require_meeting(db, meeting_id)
    motions = await db.council_members.get_motion_groups(meeting_id=meeting_id)
    votes = [vote for motion in motions for vote in motion["votes"]]
    by_matter = {}
    for motion in motions:
        mid = motion["matter_id"]
        group = by_matter.setdefault(mid, {"matter_id": mid, "votes": [], "motions": []})
        group["votes"].extend(motion["votes"])
        group["motions"].append(motion)
        group.update(tally=motion["tally"], outcome=motion["outcome"], summary_scope="last_recorded_motion")
    matters = await db.matters.get_matters_batch(list(by_matter)) if by_matter else {}
    for mid, group in by_matter.items():
        matter = matters.get(mid)
        group.update(matter_title=matter.title if matter else None,
                     matter_file=matter.matter_file if matter else None)
    return {"success": True, "meeting_id": meeting_id, "meeting_title": meeting.title,
            "meeting_date": meeting.date.isoformat() if meeting.date else None,
            "matters_with_votes": list(by_matter.values()), "total": len(votes),
            "motion_count": len(motions)}


@router.get("/council-members/{member_id}/votes")
async def get_member_votes(
    member_id: str,
    limit: int = capped_limit(100),
    db: Database = Depends(get_db)
):
    """Get voting record for a council member.

    Returns recent votes with matter context.
    """
    member = await require_council_member(db, member_id)

    voting_record = await db.council_members.get_member_voting_record(member_id, limit=limit)

    # Compute voting statistics using shared function
    vote_counts = compute_vote_tally(voting_record)

    return {
        "success": True,
        "member": member.to_dict(),
        "voting_record": voting_record,
        "total": len(voting_record),
        "statistics": vote_counts
    }


@router.get("/council-members/{member_id}/topic-profile")
async def get_member_topic_profile(member_id: str, db: Database = Depends(get_db)):
    """Per-topic voting profile for a council member.

    Aggregates the member's votes against the canonical topic vocabulary
    (matter topics plus item topics linked through the matter), with a
    yes_rate over decided (yes/no) votes per topic.
    """
    member = await require_council_member(db, member_id)

    profile = await db.council_members.get_member_topic_profile(member_id)

    return {
        "success": True,
        "member": member.to_dict(),
        "topics": profile,
        "topic_count": len(profile),
    }


@router.get("/city/{banana}/council-members")
async def get_city_council(banana: str, db: Database = Depends(get_db)):
    """Get city council roster with vote counts.

    Returns all council members for a city.
    """
    city = await require_city(db, banana)

    members = await db.council_members.get_members_by_city(banana)

    return {
        "success": True,
        "city_name": city.name,
        "state": city.state,
        "banana": banana,
        "council_members": [m.to_dict() for m in members],
        "total": len(members)
    }
