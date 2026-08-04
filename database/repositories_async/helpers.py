"""Repository helper functions for object construction and topic fetching."""

import json
from collections import defaultdict
from typing import Any, Dict, List, Optional

from database.models import (
    AgendaItem,
    AttachmentInfo,
    CityParticipation,
    Matter,
    MatterMetadata,
    Meeting,
    ParticipationInfo,
)


def deserialize_attachments(data: Any) -> List[AttachmentInfo]:
    """Deserialize JSONB attachments array to typed AttachmentInfo list.
    Strips unknown fields to handle vendor-specific extras gracefully."""
    if not data:
        return []
    known = AttachmentInfo.model_fields.keys()
    return [AttachmentInfo(**{k: v for k, v in a.items() if k in known}) for a in data]


def deserialize_metadata(data: Any) -> Optional[MatterMetadata]:
    """Deserialize JSONB metadata object to typed MatterMetadata."""
    return MatterMetadata(**data) if data else None


def deserialize_participation(data: Any) -> Optional[ParticipationInfo]:
    """Deserialize JSONB participation object to typed ParticipationInfo."""
    return ParticipationInfo(**data) if data else None


def deserialize_city_participation(data: Any) -> Optional[CityParticipation]:
    """Deserialize JSONB city participation object to typed CityParticipation."""
    return CityParticipation(**data) if data else None


def deserialize_agenda_sources(data: Any) -> Optional[List[Dict[str, str]]]:
    """Deserialize JSONB agenda_sources to typed list."""
    if not data:
        return None
    if isinstance(data, str):
        return json.loads(data)
    return data


def deserialize_extra_vendors(data: Any) -> Optional[List[Dict[str, str]]]:
    """Deserialize jurisdictions.extra_vendors JSONB to [{vendor, slug}]."""
    if not data:
        return None
    if isinstance(data, str):
        return json.loads(data)
    return data


async def fetch_topics_for_ids(
    conn,
    table: str,
    id_column: str,
    ids: List[str],
) -> Dict[str, List[str]]:
    """Batch fetch topics from a topic table, return dict mapping id -> [topics]."""
    if not ids:
        return {}

    rows = await conn.fetch(
        f"SELECT {id_column}, topic FROM {table} WHERE {id_column} = ANY($1::text[])",
        list(set(ids)),
    )

    result: Dict[str, List[str]] = defaultdict(list)
    for row in rows:
        result[row[id_column]].append(row["topic"])

    return dict(result)


def build_matter(row: Any, topics: Optional[List[str]] = None) -> Matter:
    """Construct Matter from database row with JSONB deserialization."""
    resolved_topics = topics if topics is not None else (row["canonical_topics"] or [])

    return Matter(
        id=row["id"],
        banana=row["banana"],
        matter_id=row["matter_id"],
        matter_file=row["matter_file"],
        matter_type=row["matter_type"],
        title=row["title"],
        sponsors=row["sponsors"],
        canonical_summary=row["canonical_summary"],
        canonical_topics=resolved_topics,
        attachments=deserialize_attachments(row["attachments"]),
        metadata=deserialize_metadata(row["metadata"]),
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        appearance_count=row["appearance_count"],
        status=row["status"],
        final_vote_date=row.get("final_vote_date"),
        quality_score=row.get("quality_score"),
        rating_count=row.get("rating_count", 0),
    )


def build_meeting(row: Any, topics: Optional[List[str]] = None) -> Meeting:
    """Construct Meeting from database row with JSONB deserialization."""
    participation = deserialize_participation(row["participation"])

    return Meeting(
        id=row["id"],
        banana=row["banana"],
        title=row["title"],
        date=row["date"],
        agenda_url=row["agenda_url"],
        agenda_sources=deserialize_agenda_sources(row.get("agenda_sources")),
        packet_url=row["packet_url"],
        minutes_url=row.get("minutes_url"),  # .get: not every SELECT carries it
        summary=row["summary"],
        participation=participation,
        status=row["status"],
        processing_status=row["processing_status"],
        processing_method=row["processing_method"],
        processing_time=row["processing_time"],
        committee_id=row.get("committee_id"),  # FK to committees
        topics=topics or [],
    )


def build_agenda_item(row: Any, topics: Optional[List[str]] = None) -> AgendaItem:
    """Construct AgendaItem from database row with JSONB deserialization."""
    attachments = deserialize_attachments(row["attachments"])
    sponsors = row["sponsors"] or []

    return AgendaItem(
        id=row["id"],
        meeting_id=row["meeting_id"],
        title=row["title"],
        sequence=row["sequence"],
        attachments=attachments,
        attachment_hash=row["attachment_hash"],
        body_text=row.get("body_text"),
        matter_id=row["matter_id"],
        matter_file=row["matter_file"],
        matter_type=row["matter_type"],
        agenda_number=row["agenda_number"],
        sponsors=sponsors,
        summary=row["summary"],
        topics=topics or [],
        quality_score=row.get("quality_score"),
        rating_count=row.get("rating_count", 0),
        filter_reason=row.get("filter_reason"),
    )


async def replace_entity_topics(
    conn,
    table: str,
    id_column: str,
    entity_id: str,
    topics: List[str],
) -> None:
    """Replace all topics for an entity (DELETE + INSERT pattern)."""
    await conn.execute(f"DELETE FROM {table} WHERE {id_column} = $1", entity_id)

    if topics:
        topic_records = [(entity_id, topic) for topic in topics]
        await conn.executemany(
            f"INSERT INTO {table} ({id_column}, topic) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            topic_records
        )


async def replace_entity_topics_batch(
    conn,
    table: str,
    id_column: str,
    entity_topics: Dict[str, List[str]],
) -> None:
    """Replace topics for multiple entities in batch (DELETE + INSERT)."""
    if not entity_topics:
        return

    entity_ids = list(entity_topics.keys())
    await conn.execute(f"DELETE FROM {table} WHERE {id_column} = ANY($1::text[])", entity_ids)

    all_records = [
        (entity_id, topic)
        for entity_id, topics in entity_topics.items()
        for topic in topics
    ]

    if all_records:
        await conn.executemany(
            f"INSERT INTO {table} ({id_column}, topic) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            all_records
        )
