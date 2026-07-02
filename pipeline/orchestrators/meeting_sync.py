"""Meeting Sync Orchestrator - Coordinates meeting storage workflow."""

from datetime import datetime
from typing import Optional, List, Dict, Any, TypedDict

from asyncpg import Connection

from config import get_logger
from database.id_generation import generate_meeting_id, generate_matter_id, generate_item_id, validate_matter_id
from database.models import Jurisdiction, Meeting, AgendaItem, Matter, MatterMetadata
from database.repositories_async.helpers import deserialize_attachments
from exceptions import DatabaseError, ValidationError
from pipeline.utils import hash_substantive_attachments, hash_substantive_attachments_legacy
from pipeline.orchestrators.matter_filter import MatterFilter
from pipeline.orchestrators.enqueue_decider import EnqueueDecider, MatterEnqueueDecider
from pipeline.orchestrators.vote_processor import VoteProcessor
from vendors.adapters.parsers.quality import classify_title

logger = get_logger(__name__).bind(component="meeting_sync")


class MeetingStoreStats(TypedDict, total=False):
    items_stored: int
    items_skipped_procedural: int
    matters_tracked: int
    matters_duplicate: int
    meetings_skipped: int
    appearances_created: int
    skip_reason: Optional[str]
    skipped_title: Optional[str]
    enqueue_failures: int


QUEUE_PRIORITY_BASE_SCORE = 150


class MeetingSyncOrchestrator:
    """Single entry point for all meeting sync operations."""

    def __init__(self, db):
        self.db = db
        self.matter_filter = MatterFilter()
        self.enqueue_decider = EnqueueDecider()
        self.matter_enqueue_decider = MatterEnqueueDecider()
        self.vote_processor = VoteProcessor()

    async def sync_meeting(
        self,
        meeting_dict: Dict[str, Any],
        city: Jurisdiction
    ) -> tuple[Optional[Meeting], MeetingStoreStats]:
        """Transform vendor meeting dict, store meeting and items, enqueue for processing."""
        stats: MeetingStoreStats = {
            'items_stored': 0,
            'items_skipped_procedural': 0,
            'matters_tracked': 0,
            'matters_duplicate': 0,
            'meetings_skipped': 0,
            'appearances_created': 0,
            'skip_reason': None,
            'skipped_title': None,
        }

        try:
            meeting_date = self._parse_meeting_date(meeting_dict)
            title = meeting_dict.get("title") or "Meeting"

            vendor_id = meeting_dict.get("vendor_id")

            if not vendor_id:
                logger.error("adapter returned meeting without vendor_id - check adapter output schema", city=city.banana, meeting_title=title)
                stats['meetings_skipped'] = 1
                stats['skip_reason'] = "missing_vendor_id"
                stats['skipped_title'] = title
                return None, stats

            meeting_id = generate_meeting_id(
                banana=city.banana,
                vendor_id=str(vendor_id),
                date=meeting_date or datetime.now(),
                title=title
            )

            committee_id = await self._lookup_committee_id(city.banana, meeting_dict)

            meeting_obj = Meeting(
                id=meeting_id,
                banana=city.banana,
                title=title,
                date=meeting_date,
                agenda_url=meeting_dict.get("agenda_url"),
                agenda_sources=meeting_dict.get("agenda_sources"),
                packet_url=meeting_dict.get("packet_url"),
                summary=None,
                participation=meeting_dict.get("participation"),
                status=meeting_dict.get("meeting_status"),
                processing_status="pending",
                committee_id=committee_id,
            )

            existing_meeting = await self.db.meetings.get_meeting(meeting_obj.id)
            if existing_meeting:
                # Always preserve processing state (prevents failed->pending downgrade on resync)
                meeting_obj.processing_status = existing_meeting.processing_status
                meeting_obj.processing_method = existing_meeting.processing_method
                meeting_obj.processing_time = existing_meeting.processing_time
                # Only preserve outputs if processing completed
                if existing_meeting.summary:
                    meeting_obj.summary = existing_meeting.summary
                    meeting_obj.topics = existing_meeting.topics
                    logger.debug("preserved existing summary", title=meeting_obj.title)

            agenda_items = []
            items_data = meeting_dict.get("items")
            if items_data:
                agenda_items = await self._process_agenda_items(
                    items_data, meeting_obj, stats
                )

                # Dedupe items by matter_id early - before any DB operations that use item IDs
                # This prevents FK violations when multiple items reference the same matter
                agenda_items = self.db.items.dedupe_items_by_matter(agenda_items)

            # Check if this is the first meeting for the city (before storing)
            is_first_meeting = await self._is_first_meeting_for_city(city.banana)

            pending_jobs = []
            async with self.db.pool.acquire() as conn:
                async with conn.transaction():
                    await self.db.meetings.store_meeting(meeting_obj, conn=conn)

                    if agenda_items:
                        matters_stats = await self._track_matters(
                            meeting_obj, items_data or [], agenda_items, conn=conn
                        )
                        stats['matters_tracked'] = matters_stats.get('tracked', 0)
                        stats['matters_duplicate'] = matters_stats.get('duplicate', 0)
                        stats['items_skipped_procedural'] = matters_stats.get('skipped_procedural', 0)
                        pending_jobs = matters_stats.get('pending_jobs', [])

                        # Note: we no longer null out matter_id for skipped items
                        # Skipped items still get Matter records (for FK), just no queue jobs

                        stored_count = await self.db.items.store_agenda_items(
                            meeting_obj.id, agenda_items, conn=conn
                        )
                        stats['items_stored'] = stored_count

                        # Deferred from _track_matters: copy prior-appearance
                        # summaries onto unchanged-attachment appearances. Must
                        # run AFTER store_agenda_items so the target item row
                        # exists for the UPDATE to match.
                        for pending in matters_stats.get('pending_copies', []):
                            copied = await self.db.items.copy_summary_from_prior_appearance(
                                matter_id=pending['matter_id'],
                                target_item_id=pending['target_item_id'],
                                target_meeting_id=pending['target_meeting_id'],
                                conn=conn,
                            )
                            if copied:
                                logger.debug(
                                    "reused prior-appearance summary (attachments unchanged)",
                                    matter=pending['matter_label'],
                                )

                        appearances_count = await self._create_matter_appearances(
                            meeting_obj, agenda_items, conn=conn
                        )
                        stats['appearances_created'] = appearances_count

            # Enqueue jobs after transaction commits (queue has FK to meetings)
            # Wrapped in try/except - meeting data is committed, jobs can be recovered via re-sync
            enqueue_failures = 0
            for job in pending_jobs:
                try:
                    await self._enqueue_matter_job(**job)
                except Exception as e:
                    enqueue_failures += 1
                    logger.warning(
                        "failed to enqueue matter job",
                        matter_id=job.get('matter_id'),
                        error=str(e),
                        error_type=type(e).__name__
                    )

            try:
                await self._enqueue_if_needed(
                    meeting_obj, meeting_date, agenda_items, items_data, stats,
                    chunk_audit=meeting_dict.get("chunk_audit"),
                    html_audit=meeting_dict.get("html_audit"),
                )
            except Exception as e:
                enqueue_failures += 1
                logger.warning(
                    "failed to enqueue meeting processing job",
                    meeting_id=meeting_obj.id,
                    error=str(e),
                    error_type=type(e).__name__
                )

            if enqueue_failures > 0:
                stats['enqueue_failures'] = enqueue_failures
                logger.warning(
                    "some jobs failed to enqueue - meeting data committed, jobs recoverable via re-sync",
                    meeting_id=meeting_obj.id,
                    failures=enqueue_failures
                )

            # Notify users if this was the first meeting for the city
            if is_first_meeting:
                await self._notify_city_activation(city)

            return meeting_obj, stats

        except (DatabaseError, ValidationError, ValueError) as e:
            logger.error(
                "error storing meeting",
                packet_url=meeting_dict.get('packet_url', 'unknown'),
                error=str(e),
                error_type=type(e).__name__,
            )
            raise

    def _parse_meeting_date(self, meeting_dict: Dict[str, Any]) -> Optional[datetime]:
        """Parse date string to timezone-naive datetime for DB storage."""
        date_str = meeting_dict.get("start")
        if not date_str:
            return None

        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            return dt.replace(tzinfo=None)
        except ValueError:
            pass

        for fmt in ("%m/%d/%y", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        return None

    async def _lookup_committee_id(
        self, banana: str, meeting_dict: Dict[str, Any]
    ) -> Optional[str]:
        """Find or create committee from meeting data.

        Priority:
        1. vendor_body_id — Legistar provides stable numeric body IDs
        2. body_name — CivicPlus h2 section headers, ProudCity class lists
        3. Title parsing — only for titles with clear " - " committee prefixes

        Skips committee creation for generic/non-committee names.
        """
        # Prefer vendor-provided body/committee info
        vendor_body_id = meeting_dict.get("vendor_body_id")
        meeting_title = meeting_dict.get("title", "")

        if vendor_body_id:
            # Use the title as committee name since vendor gave us a body ID
            committee_name = meeting_title.split("-")[0].strip() if "-" in meeting_title else meeting_title
            committee = await self.db.committees.find_or_create_committee(
                banana, committee_name, vendor_body_id=vendor_body_id
            )
            return committee.id

        skip_titles = {
            "meeting", "agenda", "view meeting agenda", "view agenda packet",
            "minutes", "packet", "regular meeting", "special meeting"
        }

        # Use body_name from adapter when available (e.g. CivicPlus h2 headers)
        body_name = meeting_dict.get("body_name")
        if body_name:
            if body_name.lower() not in skip_titles:
                committee = await self.db.committees.find_or_create_committee(banana, body_name)
                return committee.id
            return None

        # Fallback: parse title with " - " separator (e.g., "City Council - Regular Meeting")
        if not meeting_title or meeting_title.lower() in skip_titles:
            return None

        if " - " not in meeting_title:
            return None

        committee_name = meeting_title.split(" - ")[0].strip()
        if not committee_name or committee_name.lower() in skip_titles:
            return None

        committee = await self.db.committees.find_or_create_committee(banana, committee_name)
        return committee.id

    async def _process_agenda_items(
        self,
        items_data: List[Dict[str, Any]],
        stored_meeting: Meeting,
        stats: MeetingStoreStats
    ) -> List[AgendaItem]:
        """Build AgendaItem list, preserving existing summaries."""
        existing_items = await self.db.items.get_agenda_items(stored_meeting.id)
        existing_items_map = {item.id: item for item in existing_items}

        agenda_items = []
        seen_item_ids: set[str] = set()
        filtered_garbage = 0
        for idx, item_data in enumerate(items_data):
            # Junk-title guard. Financial/tabular content (check registers, GL-code
            # tables, rate sheets) explodes into one item per row, and it can arrive
            # from any engine in the chunker ladder or a vendor's HTML parser. Filter
            # here -- the single funnel every adapter/chunker's items pass through --
            # using the existing garbage-title classifier, rather than patching each
            # chunker. Promotes quality.classify_title from audit-only to an actual filter.
            if classify_title(item_data.get("title")):
                filtered_garbage += 1
                continue
            # Centralized item ID generation - all adapters return vendor_item_id
            # Use 'or' to handle both missing key AND explicit 0/None from vendors
            sequence = item_data.get("sequence") or (idx + 1)
            vendor_item_id = item_data.get("vendor_item_id")
            item_id = generate_item_id(stored_meeting.id, sequence, vendor_item_id)
            if item_id in seen_item_ids:
                # Section-scoped vendor numbering ("1." in consent AND regular
                # business) duplicates ids, which the items upsert's ON CONFLICT
                # would collapse into one row. Suffix by agenda order — as
                # stable as the sequence fallback.
                base_id = item_id
                n = 2
                while item_id in seen_item_ids:
                    item_id = f"{base_id}_dup{n}"
                    n += 1
                logger.warning(
                    "duplicate item id within meeting, disambiguated",
                    meeting_id=stored_meeting.id,
                    vendor_item_id=vendor_item_id,
                    item_id=item_id,
                    title=(item_data.get("title") or "")[:80],
                )
            seen_item_ids.add(item_id)

            item_attachments = deserialize_attachments(item_data.get("attachments"))
            matter_file = item_data.get("matter_file")
            matter_id_vendor = item_data.get("matter_id")

            matter_id = None
            if matter_file or matter_id_vendor:
                matter_id = generate_matter_id(
                    banana=stored_meeting.banana,
                    matter_file=matter_file,
                    matter_id=matter_id_vendor,
                )

            agenda_item = AgendaItem(
                id=item_id,
                meeting_id=stored_meeting.id,
                title=item_data.get("title", "Untitled Item"),
                sequence=sequence,
                agenda_number=item_data.get("agenda_number"),
                matter_file=matter_file,
                matter_id=matter_id,
                matter_type=item_data.get("matter_type"),
                sponsors=item_data.get("sponsors", []),
                attachments=item_attachments,
                body_text=item_data.get("body_text"),
                summary=None,
                topics=None,
            )

            existing_item = existing_items_map.get(item_id)
            if existing_item:
                # Preserve summaries from previous processing
                if existing_item.summary:
                    agenda_item.summary = existing_item.summary
                    agenda_item.topics = existing_item.topics
                # Preserve attachments if vendor didn't return them this sync
                if not agenda_item.attachments and existing_item.attachments:
                    agenda_item.attachments = existing_item.attachments

            agenda_items.append(agenda_item)

        if filtered_garbage:
            logger.info(
                "filtered garbage-titled items",
                meeting_id=stored_meeting.id,
                filtered=filtered_garbage,
                kept=len(agenda_items),
            )
        return agenda_items

    async def _track_matters(
        self,
        meeting: Meeting,
        items_data: List[Dict[str, Any]],
        agenda_items: List[AgendaItem],
        conn: Connection
    ) -> Dict[str, Any]:
        """Track matters and return stats with pending jobs to enqueue after commit."""
        stats: Dict[str, Any] = {'tracked': 0, 'duplicate': 0, 'skipped_procedural': 0, 'skipped_item_ids': set(), 'pending_jobs': [], 'pending_copies': []}

        if not items_data or not agenda_items:
            return stats

        # Index by sequence for reliable lookup (item IDs may have complex formats)
        items_map = {item.get("sequence", idx + 1): item for idx, item in enumerate(items_data)}

        for agenda_item in agenda_items:
            if not agenda_item.matter_id:
                continue

            if not validate_matter_id(agenda_item.matter_id):
                logger.error("invalid matter_id format", item_id=agenda_item.id, matter_id=agenda_item.matter_id)
                continue

            raw_item = items_map.get(agenda_item.sequence, {})
            sponsors = raw_item.get("sponsors", [])
            matter_type = raw_item.get("matter_type")
            raw_vendor_matter_id = raw_item.get("matter_id")

            # Procedural matters: still create Matter record (for FK), but skip LLM queue
            is_procedural = self.matter_filter.should_skip(matter_type)
            if is_procedural:
                stats['skipped_procedural'] += 1
                stats['skipped_item_ids'].add(agenda_item.id)
                logger.debug("procedural matter - will track but skip queue", matter=agenda_item.matter_file or raw_vendor_matter_id, matter_type=matter_type)

            existing_matter = await self.db.matters.get_matter(agenda_item.matter_id)
            attachment_hash = hash_substantive_attachments(agenda_item.attachments or [])

            if existing_matter:
                appearance_exists = await self.db.matters.has_appearance(agenda_item.matter_id, meeting.id)

                # Decide BEFORE updating tracking: the decider compares the
                # stored hash (what the canonical summary was computed from)
                # against this scrape. Writing the new hash first would erase
                # the change signal if the resulting matter job later failed.
                should_enqueue, skip_reason = False, None
                if not is_procedural:
                    should_enqueue, skip_reason = self.matter_enqueue_decider.should_enqueue_matter(
                        existing_matter=existing_matter,
                        current_attachment_hash=attachment_hash,
                        has_attachments=bool(agenda_item.attachments),
                        current_attachment_hash_legacy=hash_substantive_attachments_legacy(
                            agenda_item.attachments or []
                        ),
                    )

                # Persist the hash only on a confirmed-unchanged scrape: a
                # semantic no-op that upgrades legacy-format hashes in place.
                # On change (or failure to summarize), the stored hash keeps
                # pointing at the summarized state until process_matter
                # succeeds and writes the new one.
                await self.db.matters.update_matter_tracking(
                    matter_id=agenda_item.matter_id,
                    meeting_date=meeting.date,
                    attachments=agenda_item.attachments,
                    attachment_hash=attachment_hash if skip_reason == "attachments_unchanged" else None,
                    increment_appearance_count=not appearance_exists,
                    conn=conn
                )
                stats['duplicate'] += 1
                if not appearance_exists and (agenda_item.matter_file or raw_vendor_matter_id):
                    logger.info("matter new appearance", matter=agenda_item.matter_file or raw_vendor_matter_id, matter_type=matter_type)

                # Skip queueing for procedural matters
                if is_procedural:
                    continue

                if should_enqueue:
                    item_ids = await self._collect_item_ids_for_matter(agenda_item.matter_id)
                    stats['pending_jobs'].append({
                        'matter_id': agenda_item.matter_id,
                        'meeting_id': meeting.id,
                        'item_ids': item_ids,
                        'banana': meeting.banana,
                        'meeting_date': meeting.date
                    })
                elif skip_reason == "attachments_unchanged":
                    # Defer the copy: the target items row does not exist yet
                    # (store_agenda_items runs later in the same transaction).
                    # The caller executes these after store_agenda_items so the
                    # UPDATE inside copy_summary_from_prior_appearance matches.
                    stats['pending_copies'].append({
                        'matter_id': agenda_item.matter_id,
                        'target_item_id': agenda_item.id,
                        'target_meeting_id': meeting.id,
                        'matter_label': agenda_item.matter_file or raw_vendor_matter_id,
                    })
            else:
                if not agenda_item.matter_file and not raw_vendor_matter_id and not agenda_item.title:
                    continue

                matter_obj = Matter(
                    id=agenda_item.matter_id,
                    banana=meeting.banana,
                    matter_id=raw_vendor_matter_id,
                    matter_file=agenda_item.matter_file,
                    matter_type=matter_type,
                    title=agenda_item.title,
                    sponsors=sponsors,
                    canonical_summary=None,
                    canonical_topics=None,
                    attachments=agenda_item.attachments,
                    metadata=MatterMetadata(attachment_hash=attachment_hash),
                    first_seen=meeting.date,
                    last_seen=meeting.date,
                    appearance_count=1,
                )

                await self.db.matters.store_matter(matter_obj, conn=conn)
                stats['tracked'] += 1

                if agenda_item.matter_file or raw_vendor_matter_id:
                    logger.info("new matter tracked", matter=agenda_item.matter_file or raw_vendor_matter_id, matter_type=matter_type, sponsor_count=len(sponsors))

                # Skip queueing for procedural matters
                if is_procedural:
                    continue

                if agenda_item.attachments:
                    stats['pending_jobs'].append({
                        'matter_id': agenda_item.matter_id,
                        'meeting_id': meeting.id,
                        'item_ids': [agenda_item.id],
                        'banana': meeting.banana,
                        'meeting_date': meeting.date
                    })

                if sponsors:
                    await self.db.council_members.link_sponsors_to_matter(
                        banana=meeting.banana, matter_id=agenda_item.matter_id, sponsor_names=sponsors, appeared_at=meeting.date, conn=conn
                    )

            votes = raw_item.get("votes", [])
            if votes:
                await self.db.council_members.record_votes_for_matter(
                    banana=meeting.banana, matter_id=agenda_item.matter_id, meeting_id=meeting.id, votes=votes, vote_date=meeting.date, conn=conn
                )
                result = self.vote_processor.process_votes(votes)
                await self.db.matters.update_appearance_outcome(
                    matter_id=agenda_item.matter_id, meeting_id=meeting.id, item_id=agenda_item.id, vote_outcome=result["outcome"], vote_tally=result["tally"], conn=conn
                )

        return stats

    async def _create_matter_appearances(
        self,
        meeting: Meeting,
        agenda_items: List[AgendaItem],
        conn: Connection
    ) -> int:
        """Create matter_appearances after items are stored."""
        count = 0
        committee = meeting.title.split("-")[0].strip() if meeting.title else None

        for agenda_item in agenda_items:
            if not agenda_item.matter_id:
                continue

            await self.db.matters.create_appearance(
                matter_id=agenda_item.matter_id,
                meeting_id=meeting.id,
                item_id=agenda_item.id,
                appeared_at=meeting.date,
                committee=committee,
                committee_id=meeting.committee_id,
                sequence=agenda_item.sequence,
                conn=conn
            )
            count += 1

        return count

    async def _enqueue_if_needed(
        self,
        stored_meeting: Meeting,
        meeting_date: Optional[datetime],
        agenda_items: List[AgendaItem],
        items_data: Optional[List[Dict[str, Any]]],
        stats: MeetingStoreStats,
        chunk_audit: Optional[Dict[str, Any]] = None,
        html_audit: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Enqueue meeting for LLM processing if criteria are met."""
        should_enqueue, skip_reason = self.enqueue_decider.should_enqueue(stored_meeting, agenda_items, bool(agenda_items))

        if not should_enqueue:
            if skip_reason:
                logger.debug("skipping enqueue", reason=skip_reason, meeting_id=stored_meeting.id)
            return

        priority = self.enqueue_decider.calculate_priority(meeting_date)

        await self.db.queue.enqueue_job(
            source_url=f"meeting://{stored_meeting.id}",
            job_type="meeting",
            payload={"meeting_id": stored_meeting.id},
            meeting_id=stored_meeting.id,
            priority=priority,
            banana=stored_meeting.banana,
            processing_metadata=(
                {
                    k: v
                    for k, v in (("chunk", chunk_audit), ("html", html_audit))
                    if v
                }
                or None
            ),
        )

        logger.info("enqueued meeting for processing", meeting_id=stored_meeting.id, priority=priority)

    async def _enqueue_matter_job(
        self,
        matter_id: str,
        meeting_id: str,
        item_ids: List[str],
        banana: str,
        meeting_date: Optional[datetime]
    ) -> None:
        priority = self.matter_enqueue_decider.calculate_priority(meeting_date)

        await self.db.queue.enqueue_job(
            source_url=f"matter://{matter_id}",
            job_type="matter",
            payload={"matter_id": matter_id, "meeting_id": meeting_id, "item_ids": item_ids},
            meeting_id=meeting_id,
            banana=banana,
            priority=priority,
        )

        logger.info("enqueued matter for processing", matter_id=matter_id, priority=priority)

    async def _collect_item_ids_for_matter(self, matter_id: str) -> List[str]:
        items = await self.db.items.get_all_items_for_matter(matter_id)
        return [item.id for item in items]

    async def _is_first_meeting_for_city(self, banana: str) -> bool:
        """Check if city has no existing meetings (first sync detection)."""
        meetings = await self.db.meetings.get_meetings_for_city(banana, limit=1)
        return len(meetings) == 0

    async def _notify_city_activation(self, city: Jurisdiction) -> None:
        """Notify users who signed up for alerts when city first gets data.

        Sends "city now available" email and updates city_request status.
        """
        try:
            # Get alerts for this city
            alerts = await self.db.userland.get_alerts_for_city(city.banana)
            if not alerts:
                logger.debug("no alerts for newly activated city", banana=city.banana)
            else:
                # Import here to avoid circular dependency
                from userland.email.transactional import send_city_available_email

                # Get user info and send emails
                for alert in alerts:
                    user = await self.db.userland.get_user(alert.user_id)
                    if user:
                        await send_city_available_email(
                            email=user.email,
                            user_name=user.name,
                            city_name=city.name,
                            state=city.state,
                            banana=city.banana
                        )

                logger.info(
                    "sent city activation emails",
                    banana=city.banana,
                    alert_count=len(alerts)
                )

            # Update city_request status to 'added'
            await self.db.userland.update_city_request_status(
                banana=city.banana,
                status='added',
                notes=f'First meeting synced {datetime.now().isoformat()}'
            )

        except Exception as e:
            # Don't fail the sync for notification errors
            logger.warning(
                "city activation notification failed",
                banana=city.banana,
                error=str(e)
            )
