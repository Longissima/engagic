"""
Meeting API routes
"""

from fastapi import APIRouter, HTTPException, Depends
from server.models.requests import ProcessRequest
from server.services.meeting import get_meeting_with_items
from server.metrics import metrics
from server.dependencies import get_db
from database.db_postgres import Database

from config import get_logger

logger = get_logger(__name__)


router = APIRouter(prefix="/api")


@router.get("/meeting/{meeting_id}")
async def get_meeting(meeting_id: str, db: Database = Depends(get_db)):
    """Get a single meeting by ID - optimized endpoint to avoid fetching all city meetings"""
    try:
        meeting = await db.get_meeting(meeting_id)
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")

        metrics.page_views.labels(page_type='meeting').inc()

        meeting_dict = await get_meeting_with_items(meeting, db)
        city = await db.get_city(banana=meeting.banana)

        return {
            "success": True,
            "meeting": meeting_dict,
            "city_name": city.name if city else None,
            "state": city.state if city else None,
            "banana": meeting.banana,
            "participation": city.participation.model_dump(exclude_none=True) if city and city.participation else None,
        }

    except HTTPException:
        raise
    except Exception:
        logger.exception("error fetching meeting", meeting_id=meeting_id)
        raise HTTPException(status_code=500, detail="Error retrieving meeting")


@router.post("/process-agenda")
async def process_agenda(request: ProcessRequest, db: Database = Depends(get_db)):
    """INFO-ONLY: Check agenda processing status.

    This endpoint does NOT trigger on-demand processing. All processing happens
    via the background daemon (conductor.py). Returns estimated wait time.

    To ensure your city gets processed: watch the city to add it to priority queue.
    Priority cities are synced every 24 hours automatically.
    """
    try:
        return {
            "success": False,
            "message": "Summary not yet available - processing in background",
            "cached": False,
            "packet_url": request.packet_url,
            "estimated_wait_minutes": 10,
            "note": "Watch this city to ensure priority weekly syncing",
        }

    except Exception:
        logger.exception("error retrieving agenda", packet_url=request.packet_url)
        raise HTTPException(
            status_code=500,
            detail="We're having trouble loading this agenda. Watch this city to ensure priority weekly syncing."
        )


@router.get("/random-meeting-with-items")
async def get_random_meeting_with_items(db: Database = Depends(get_db)):
    """Get a random meeting that has high-quality item-level summaries"""
    try:
        result = await db.get_random_meeting_with_items()

        if not result:
            raise HTTPException(
                status_code=404,
                detail="No meetings with item summaries available yet",
            )

        return {
            "success": True,
            "meeting": result,
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("error getting random meeting with items")
        raise HTTPException(status_code=500, detail="Error retrieving meeting")


@router.get("/meetings/{meeting_id}/minutes")
async def get_meeting_minutes(meeting_id: str, db: Database = Depends(get_db)):
    """Corpus references for all observed minutes revisions, newest first."""
    from server.utils.validation import require_meeting
    await require_meeting(db, meeting_id)
    documents = await db.document_blobs.get_minutes_documents(meeting_id)
    current = documents[0] if documents else {}
    older_ready = next((doc for doc in documents[1:] if doc["text_ready"]), None)
    return {"success": True, "meeting_id": meeting_id, "documents": documents,
            "current_content_sha256": current.get("content_sha256"),
            "current_text_ready": current.get("text_ready", False),
            "older_text_available": older_ready is not None,
            "older_text_content_sha256": older_ready["content_sha256"] if older_ready else None,
            "fallback_used": False}
