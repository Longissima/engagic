"""Committee API routes - handles committees, membership, and committee voting history."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional

from database.db_postgres import Database
from server.dependencies import get_db
from server.utils.validation import require_city

router = APIRouter(prefix="/api")


@router.get("/city/{banana}/committees")
async def get_city_committees(
    banana: str,
    status: Optional[str] = Query(None, description="Filter by status: active, inactive"),
    db: Database = Depends(get_db)
):
    """Get all committees for a city.

    Returns list of committees with member counts.
    """
    city = await require_city(db, banana)

    committees = await db.committees.get_committees_by_city(banana, status=status)

    # Batch fetch member counts (single query instead of N+1)
    committee_ids = [c.id for c in committees]
    member_counts = await db.committees.get_committee_member_counts(committee_ids, active_only=True)

    committees_with_counts = [
        {
            "id": committee.id,
            "name": committee.name,
            "description": committee.description,
            "status": committee.status,
            "member_count": member_counts.get(committee.id, 0),
            "created_at": committee.created_at.isoformat() if committee.created_at else None,
        }
        for committee in committees
    ]

    return {
        "success": True,
        "city_name": city.name,
        "state": city.state,
        "banana": banana,
        "committees": committees_with_counts,
        "total": len(committees_with_counts)
    }


@router.get("/committees/{committee_id}")
async def get_committee(committee_id: str, db: Database = Depends(get_db)):
    """Get committee details.

    Returns committee info with current roster.
    """
    committee = await db.committees.get_committee_by_id(committee_id)
    if not committee:
        raise HTTPException(status_code=404, detail="Committee not found")

    # Get current members
    members = await db.committees.get_committee_members(committee_id, active_only=True)

    # Get city info
    city = await db.jurisdictions.get_city(committee.banana)

    return {
        "success": True,
        "committee": {
            "id": committee.id,
            "name": committee.name,
            "description": committee.description,
            "status": committee.status,
            "banana": committee.banana,
            "created_at": committee.created_at.isoformat() if committee.created_at else None,
        },
        "city_name": city.name if city else None,
        "state": city.state if city else None,
        "members": members,
        "member_count": len(members)
    }


@router.get("/committees/{committee_id}/members")
async def get_committee_members(
    committee_id: str,
    active_only: bool = Query(True, description="Only show current members"),
    as_of: Optional[str] = Query(None, description="Historical date (ISO format)"),
    db: Database = Depends(get_db)
):
    """Get committee membership roster.

    Supports historical queries with as_of parameter.
    """
    committee = await db.committees.get_committee_by_id(committee_id)
    if not committee:
        raise HTTPException(status_code=404, detail="Committee not found")

    # Parse as_of date if provided
    as_of_date = None
    if as_of:
        try:
            as_of_date = datetime.fromisoformat(as_of.replace('Z', '+00:00'))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use ISO format.")

    members = await db.committees.get_committee_members(
        committee_id,
        active_only=active_only if not as_of_date else False,
        as_of=as_of_date
    )

    return {
        "success": True,
        "committee_id": committee_id,
        "committee_name": committee.name,
        "as_of": as_of,
        "members": members,
        "total": len(members)
    }


@router.get("/committees/{committee_id}/votes")
async def get_committee_votes(
    committee_id: str,
    limit: int = Query(50, ge=1, le=100),
    db: Database = Depends(get_db)
):
    """Get voting history for a committee.

    Returns matters voted on with outcomes and tallies.
    """
    committee = await db.committees.get_committee_by_id(committee_id)
    if not committee:
        raise HTTPException(status_code=404, detail="Committee not found")

    vote_history = await db.committees.get_committee_vote_history(committee_id, limit=limit)

    return {
        "success": True,
        "committee_id": committee_id,
        "committee_name": committee.name,
        "votes": vote_history,
        "total": len(vote_history)
    }


@router.get("/council-members/{member_id}/committees")
async def get_member_committees(
    member_id: str,
    active_only: bool = Query(True, description="Only show current assignments"),
    db: Database = Depends(get_db)
):
    """Get committees a council member serves on.

    Returns list of committee assignments with roles.
    """
    # Verify member exists
    member = await db.council_members.get_member_by_id(member_id)
    if not member:
        raise HTTPException(status_code=404, detail="Council member not found")

    committees = await db.committees.get_member_committees(member_id, active_only=active_only)

    return {
        "success": True,
        "member_id": member_id,
        "member_name": member.name,
        "committees": committees,
        "total": len(committees)
    }
