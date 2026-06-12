"""Vote API routes - handles vote records, tallies, and council member voting history."""

from server.utils.validation import capped_limit
from fastapi import APIRouter, Depends

from database.db_postgres import Database
from database.vote_utils import compute_vote_tally, determine_vote_outcome
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
    """Get all votes on a matter across all meetings.

    Returns individual votes grouped by meeting/committee, plus aggregate tally.
    """
    matter = await require_matter(db, matter_id)

    votes = await db.council_members.get_votes_for_matter(matter_id)
    tally = await db.council_members.get_vote_tally_for_matter(matter_id)
    outcomes = await db.matters.get_matter_vote_outcomes(matter_id)

    # Group votes by meeting with committee context
    votes_by_meeting: dict[str, dict] = {}
    for vote in votes:
        mid = vote.meeting_id
        if mid not in votes_by_meeting:
            votes_by_meeting[mid] = {
                "meeting_id": mid,
                "votes": [],
                "committee": None,
                "meeting_date": None
            }
        votes_by_meeting[mid]["votes"].append(vote.to_dict())

    # Enrich with meeting and committee info
    if votes_by_meeting:
        meeting_ids = list(votes_by_meeting.keys())
        async with db.pool.acquire() as conn:
            meeting_info = await conn.fetch(
                """
                SELECT
                    m.id as meeting_id,
                    m.title as meeting_title,
                    m.date as meeting_date,
                    ma.committee,
                    ma.committee_id,
                    ma.vote_outcome,
                    ma.vote_tally,
                    cm.name as committee_name
                FROM meetings m
                LEFT JOIN matter_appearances ma ON ma.meeting_id = m.id AND ma.matter_id = $1
                LEFT JOIN committees cm ON ma.committee_id = cm.id
                WHERE m.id = ANY($2)
                ORDER BY m.date ASC
                """,
                matter_id, meeting_ids
            )

        for row in meeting_info:
            mid = row["meeting_id"]
            if mid in votes_by_meeting:
                votes_by_meeting[mid]["meeting_title"] = row["meeting_title"]
                votes_by_meeting[mid]["meeting_date"] = row["meeting_date"].isoformat() if row["meeting_date"] else None
                votes_by_meeting[mid]["committee"] = row["committee_name"] or row["committee"]
                votes_by_meeting[mid]["committee_id"] = row["committee_id"]
                votes_by_meeting[mid]["vote_outcome"] = row["vote_outcome"]
                votes_by_meeting[mid]["vote_tally"] = row["vote_tally"]

                # Compute tally from individual votes for this meeting
                meeting_votes = votes_by_meeting[mid]["votes"]
                votes_by_meeting[mid]["computed_tally"] = compute_vote_tally(meeting_votes)

    metrics.matter_engagement.labels(action='votes').inc()

    return {
        "success": True,
        "matter_id": matter_id,
        "matter_title": matter.title,
        "votes": [v.to_dict() for v in votes],
        "votes_by_meeting": list(votes_by_meeting.values()),
        "tally": tally,
        "outcomes": outcomes
    }


@router.get("/meetings/{meeting_id}/votes")
async def get_meeting_votes(meeting_id: str, db: Database = Depends(get_db)):
    """Get all votes cast in a meeting.

    Returns votes grouped by matter.
    """
    meeting = await require_meeting(db, meeting_id)

    votes = await db.council_members.get_votes_for_meeting(meeting_id)

    # Group votes by matter
    votes_by_matter: dict[str, list] = {}
    for vote in votes:
        mid = vote.matter_id
        if mid not in votes_by_matter:
            votes_by_matter[mid] = []
        votes_by_matter[mid].append(vote.to_dict())

    # Get matter details using repository (batch fetch)
    matter_ids = list(votes_by_matter.keys())
    matters_batch = await db.matters.get_matters_batch(matter_ids) if matter_ids else {}
    matters_data = {
        mid: {"title": m.title, "matter_file": m.matter_file}
        for mid, m in matters_batch.items()
    }

    # Build response grouped by matter
    matters_with_votes = []
    for mid, matter_votes in votes_by_matter.items():
        matter_info = matters_data.get(mid, {})

        # Use shared vote tally and outcome functions
        tally = compute_vote_tally(matter_votes)
        outcome = determine_vote_outcome(tally)

        matters_with_votes.append({
            "matter_id": mid,
            "matter_title": matter_info.get("title"),
            "matter_file": matter_info.get("matter_file"),
            "votes": matter_votes,
            "tally": tally,
            "outcome": outcome
        })

    return {
        "success": True,
        "meeting_id": meeting_id,
        "meeting_title": meeting.title,
        "meeting_date": meeting.date.isoformat() if meeting.date else None,
        "matters_with_votes": matters_with_votes,
        "total": len(votes)
    }


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
