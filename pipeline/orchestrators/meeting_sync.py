"""Meeting Sync Orchestrator - Coordinates meeting storage workflow."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, TypedDict

from asyncpg import Connection

from config import get_logger
from database.id_generation import (
    generate_meeting_id,
    generate_matter_id,
    generate_item_id,
    normalize_sponsor_name,
    validate_matter_id,
)
from database.models import Jurisdiction, Meeting, AgendaItem, Matter, MatterMetadata
from database.repositories_async.helpers import deserialize_attachments
from exceptions import DatabaseError, ValidationError
from parsing.identifiers import extract_identifier
from pipeline.utils import (
    MatterNoWorkReason,
    MatterWorkSnapshot,
    matter_no_work_version,
    meeting_work_version,
)
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
    activation_checked: bool
    activation_notifications: int


@dataclass(frozen=True, slots=True)
class _MatterSyncSnapshot:
    """Set-wise read model held under one meeting transaction."""

    matters: Mapping[str, Matter]
    prior_appearances: Mapping[str, List[AgendaItem]]


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
        city: Jurisdiction,
        *,
        check_city_activation: bool = False,
    ) -> tuple[Optional[Meeting], MeetingStoreStats]:
        """Transform vendor meeting dict, store meeting and items, enqueue for processing."""
        stats: MeetingStoreStats = {
            "items_stored": 0,
            "items_skipped_procedural": 0,
            "matters_tracked": 0,
            "matters_duplicate": 0,
            "meetings_skipped": 0,
            "appearances_created": 0,
            "skip_reason": None,
            "skipped_title": None,
            "activation_checked": False,
            "activation_notifications": 0,
        }

        try:
            meeting_date = self._parse_meeting_date(meeting_dict)
            title = meeting_dict.get("title") or "Meeting"

            vendor_id = meeting_dict.get("vendor_id")

            if not vendor_id:
                logger.error(
                    "adapter returned meeting without vendor_id - check adapter output schema",
                    city=city.banana,
                    meeting_title=title,
                )
                stats["meetings_skipped"] = 1
                stats["skip_reason"] = "missing_vendor_id"
                stats["skipped_title"] = title
                return None, stats

            meeting_id = generate_meeting_id(
                banana=city.banana,
                vendor_id=str(vendor_id),
                date=meeting_date,
                title=title,
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
                minutes_url=meeting_dict.get("minutes_url"),
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

            matters_stats: Dict[str, Any] = {}
            async with self.db.pool.acquire() as conn:
                async with conn.transaction():
                    is_first_meeting = False
                    if check_city_activation:
                        is_first_meeting = await self._claim_city_activation(
                            city.banana,
                            conn,
                        )
                        stats["activation_checked"] = True
                    await self.db.meetings.store_meeting(meeting_obj, conn=conn)

                    if agenda_items:
                        # store_meeting owns the meeting row lock. Discover old
                        # links without item locks, then lock the sorted old+new
                        # matter union before any item row. This prevents
                        # concurrent A -> B / B -> A relinks from inverting.
                        locked_meeting = await self.db.meetings.get_meeting(
                            meeting_obj.id,
                            conn=conn,
                            lock_for_update=True,
                        )
                        if locked_meeting is None:
                            raise DatabaseError(
                                "meeting disappeared before item persistence: "
                                f"{meeting_obj.id}"
                            )
                        pre_upsert_links = await self.db.items.get_item_matter_links(
                            meeting_obj.id,
                            conn=conn,
                        )
                        affected_matter_ids = self._affected_matter_ids(
                            pre_upsert_links,
                            agenda_items,
                        )
                        matters_stats = await self._track_matters(
                            locked_meeting,
                            items_data or [],
                            agenda_items,
                            affected_matter_ids=affected_matter_ids,
                            conn=conn,
                        )
                        stats["matters_tracked"] = matters_stats.get("tracked", 0)
                        stats["matters_duplicate"] = matters_stats.get("duplicate", 0)
                        stats["items_skipped_procedural"] = matters_stats.get(
                            "skipped_procedural", 0
                        )

                        # Note: we no longer null out matter_id for skipped items
                        # Skipped items still get Matter records (for FK), just no queue jobs

                        stored_count = await self.db.items.store_agenda_items(
                            meeting_obj.id, agenda_items, conn=conn
                        )
                        stats["items_stored"] = stored_count

                        appearance_changes = (
                            await self._reconcile_matter_appearances(
                                locked_meeting,
                                matters_stats.get("appearance_outcomes", {}),
                                affected_matter_ids=set(
                                    matters_stats.get("affected_matter_ids", set())
                                ),
                                observed_votes=matters_stats.get(
                                    "observed_votes", {}
                                ),
                                conn=conn,
                            )
                        )
                        stats["appearances_created"] = appearance_changes["inserted"]

                    if is_first_meeting:
                        stats[
                            "activation_notifications"
                        ] = await self._enqueue_city_activation(city, conn)

                    # Publication is based only on rows that survived every
                    # UPSERT/COALESCE/freeze-on-summary rule above. Lock domain
                    # aggregates before re-reading their item appearances.
                    meeting_obj = await self._publish_authoritative_work(
                        meeting_id=meeting_obj.id,
                        affected_matter_ids=set(
                            matters_stats.get("affected_matter_ids", set())
                        ),
                        procedural_matter_ids=set(
                            matters_stats.get("procedural_matter_ids", set())
                        ),
                        conn=conn,
                        publish_meeting=True,
                        chunk_audit=meeting_dict.get("chunk_audit"),
                        html_audit=meeting_dict.get("html_audit"),
                    )

            return meeting_obj, stats

        except (DatabaseError, ValidationError, ValueError) as e:
            logger.error(
                "error storing meeting",
                packet_url=meeting_dict.get("packet_url", "unknown"),
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
            committee_name = (
                meeting_title.split("-")[0].strip()
                if "-" in meeting_title
                else meeting_title
            )
            committee = await self.db.committees.find_or_create_committee(
                banana, committee_name, vendor_body_id=vendor_body_id
            )
            return committee.id

        skip_titles = {
            "meeting",
            "agenda",
            "view meeting agenda",
            "view agenda packet",
            "minutes",
            "packet",
            "regular meeting",
            "special meeting",
        }

        # Use body_name from adapter when available (e.g. CivicPlus h2 headers)
        body_name = meeting_dict.get("body_name")
        if body_name:
            if body_name.lower() not in skip_titles:
                committee = await self.db.committees.find_or_create_committee(
                    banana, body_name
                )
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

        committee = await self.db.committees.find_or_create_committee(
            banana, committee_name
        )
        return committee.id

    async def attach_items(
        self,
        stored_meeting: Meeting,
        items_data: List[Dict[str, Any]],
        *,
        expected_desired_version: Optional[str],
        expected_claim_token: str,
    ) -> int:
        """Attach freshly-manufactured items to an already-stored meeting.

        The processor's entry into the item funnel: when shape is produced at
        claim time (sync deferred chunking, or sync's chunk found nothing),
        the exact same pipeline runs -- ID generation, junk-title filter,
        matter tracking, snapshot-preserving store, prior-appearance summary
        copies, appearances -- minus the meeting store and meeting-job
        enqueue, which already happened. Matter jobs discovered here still
        enqueue, so the matter lane behaves identically regardless of where
        shape was born. Returns the number of items stored.
        """
        stats: MeetingStoreStats = {
            "items_stored": 0,
            "items_skipped_procedural": 0,
            "matters_tracked": 0,
            "matters_duplicate": 0,
            "meetings_skipped": 0,
            "appearances_created": 0,
            "skip_reason": None,
        }
        agenda_items = await self._process_agenda_items(
            items_data, stored_meeting, stats
        )
        agenda_items = self.db.items.dedupe_items_by_matter(agenda_items)
        if not agenda_items:
            return 0

        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                # attach_items does not write the meeting row, so acquire its
                # domain lock explicitly before _track_matters takes any
                # matter/item locks. The later publication read refreshes it.
                locked_meeting = await self.db.meetings.get_meeting(
                    stored_meeting.id,
                    conn=conn,
                    lock_for_update=True,
                )
                if locked_meeting is None:
                    raise DatabaseError(
                        "meeting disappeared while attaching items: "
                        f"{stored_meeting.id}"
                    )
                pre_upsert_links = await self.db.items.get_item_matter_links(
                    stored_meeting.id,
                    conn=conn,
                )
                affected_matter_ids = self._affected_matter_ids(
                    pre_upsert_links,
                    agenda_items,
                )
                matters_stats = await self._track_matters(
                    locked_meeting,
                    items_data or [],
                    agenda_items,
                    affected_matter_ids=affected_matter_ids,
                    conn=conn,
                )

                stored_count = await self.db.items.store_agenda_items(
                    stored_meeting.id, agenda_items, conn=conn
                )

                await self._reconcile_matter_appearances(
                    locked_meeting,
                    matters_stats.get("appearance_outcomes", {}),
                    affected_matter_ids=set(
                        matters_stats.get("affected_matter_ids", set())
                    ),
                    observed_votes=matters_stats.get("observed_votes", {}),
                    conn=conn,
                )

                await self._publish_authoritative_work(
                    meeting_id=stored_meeting.id,
                    affected_matter_ids=set(
                        matters_stats.get("affected_matter_ids", set())
                    ),
                    procedural_matter_ids=set(
                        matters_stats.get("procedural_matter_ids", set())
                    ),
                    conn=conn,
                    publish_meeting=False,
                )

                # Shape manufacture is part of an already-claimed meeting job,
                # not an independent sync. Validate the original queue
                # descriptor and exact owner in this same transaction after the
                # canonical domain/item/matter locks. If sync or a same-version
                # re-owner won while chunking was in flight, every attachment,
                # appearance, and matter-publication write above rolls back.
                desired_state = await self.db.queue.lock_desired_state(
                    f"meeting://{stored_meeting.id}",
                    conn=conn,
                )
                desired_version = (
                    desired_state.get("work_version")
                    if desired_state is not None
                    else None
                )
                if desired_version != expected_desired_version:
                    raise DatabaseError(
                        f"meeting {stored_meeting.id} desired work was superseded "
                        f"({expected_desired_version} -> {desired_version})"
                    )
                desired_claim_token = (
                    desired_state.get("claim_token")
                    if desired_state is not None
                    else None
                )
                if (
                    desired_state is None
                    or desired_state.get("status") != "processing"
                    or desired_claim_token is None
                    or str(desired_claim_token) != expected_claim_token
                ):
                    raise DatabaseError(
                        f"meeting {stored_meeting.id} queue claim was superseded"
                    )

        logger.info(
            "attached manufactured items to meeting",
            meeting_id=stored_meeting.id,
            items_stored=stored_count,
            matters_tracked=matters_stats.get("tracked", 0),
        )
        return stored_count

    async def _process_agenda_items(
        self,
        items_data: List[Dict[str, Any]],
        stored_meeting: Meeting,
        stats: MeetingStoreStats,
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
            matter_type = item_data.get("matter_type")

            # Last resort: read a durable identifier out of the agenda text
            # itself. Many vendors publish no matter key at all, yet the body
            # cites a contract or case number that recurs across committee,
            # council and later amendments. Deriving it here rather than in each
            # adapter keeps it self-healing -- the items upsert overwrites
            # matter_file from every sync, so a value that is not re-derived on
            # each pass would be silently erased.
            if not matter_file and not matter_id_vendor:
                derived = extract_identifier(
                    item_data.get("title"), item_data.get("body_text")
                )
                if derived:
                    matter_file, matter_type = derived

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
                matter_type=matter_type,
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

    async def _load_matter_sync_snapshot(
        self,
        affected_matter_ids: set[str],
        conn: Connection,
    ) -> _MatterSyncSnapshot:
        """Load all decision inputs once, in global matter -> items lock order."""
        matter_ids = sorted(affected_matter_ids)
        if not matter_ids:
            return _MatterSyncSnapshot({}, {})

        matters = await self.db.matters.get_matters_for_sync_snapshot(
            matter_ids,
            conn=conn,
        )
        prior_appearances = await self.db.items.get_all_items_for_matters(
            matter_ids,
            conn=conn,
            lock_for_update=True,
        )
        return _MatterSyncSnapshot(
            matters=matters,
            prior_appearances=prior_appearances,
        )

    @staticmethod
    def _affected_matter_ids(
        pre_upsert_links: Mapping[str, str],
        proposed_items: List[AgendaItem],
    ) -> set[str]:
        """Return every aggregate whose retained work an item UPSERT can change."""
        return {
            matter_id
            for matter_id in pre_upsert_links.values()
            if matter_id
        } | {
            item.matter_id
            for item in proposed_items
            if item.matter_id and validate_matter_id(item.matter_id)
        }

    async def _track_matters(
        self,
        meeting: Meeting,
        items_data: List[Dict[str, Any]],
        agenda_items: List[AgendaItem],
        *,
        affected_matter_ids: set[str],
        conn: Connection,
    ) -> Dict[str, Any]:
        """Persist matter bookkeeping and identify aggregates for publication.

        This phase deliberately does not decide or publish desired work. Item
        UPSERTs happen later and may retain older mutable fields under the
        freeze-on-summary rule. The publication phase re-reads the committed
        transaction view after those writes and derives versions from that
        authoritative state.
        """
        stats: Dict[str, Any] = {
            "tracked": 0,
            "duplicate": 0,
            "skipped_procedural": 0,
            "skipped_item_ids": set(),
            "procedural_matter_ids": set(),
            "appearance_outcomes": {},
            "observed_votes": {},
            "affected_matter_ids": set(affected_matter_ids),
        }

        if not items_data or not agenda_items:
            return stats

        # Index by sequence for reliable lookup (item IDs may have complex formats)
        items_map = {
            item.get("sequence") or (idx + 1): item
            for idx, item in enumerate(items_data)
        }
        snapshot = await self._load_matter_sync_snapshot(
            affected_matter_ids,
            conn,
        )

        for agenda_item in agenda_items:
            if not agenda_item.matter_id:
                continue

            if not validate_matter_id(agenda_item.matter_id):
                logger.error(
                    "invalid matter_id format",
                    item_id=agenda_item.id,
                    matter_id=agenda_item.matter_id,
                )
                continue

            raw_item = items_map.get(agenda_item.sequence, {})
            sponsors = raw_item.get("sponsors", [])
            # Generic identifiers are derived into the AgendaItem by the
            # shared funnel, so adapter input alone is not authoritative here.
            # Without this fallback a new ``Contract 1234`` aggregate is
            # created with a null type even though the derived item carries it.
            matter_type = raw_item.get("matter_type") or agenda_item.matter_type
            raw_vendor_matter_id = raw_item.get("matter_id")
            # The vendor's own lifecycle verdict ("Placed On File", "Passed").
            # None for vendors that publish no status; never written in that
            # case, so a silent vendor cannot erase a known status.
            vendor_matter_status = raw_item.get("matter_status")
            if "votes" in raw_item:
                stats["observed_votes"][agenda_item.matter_id] = raw_item.get(
                    "votes"
                ) or []

            # Procedural matters: still create Matter record (for FK), but skip LLM queue
            is_procedural = self.matter_filter.should_skip(matter_type)
            if is_procedural:
                stats["skipped_procedural"] += 1
                stats["skipped_item_ids"].add(agenda_item.id)
                stats["procedural_matter_ids"].add(agenda_item.matter_id)
                logger.debug(
                    "procedural matter - will track but skip queue",
                    matter=agenda_item.matter_file or raw_vendor_matter_id,
                    matter_type=matter_type,
                )

            existing_matter = snapshot.matters.get(agenda_item.matter_id)

            if existing_matter:
                # Do not update aggregate tracking from proposed values here.
                # A summarized item can retain its old title/body/attachments
                # in the later item UPSERT. The authoritative publication
                # phase performs the tracking write from retained appearances.
                stats["duplicate"] += 1
                # A matter's status changes long after it is first tracked --
                # the kill vote is usually its last appearance, not its first.
                # This is the only path that revisits an existing aggregate, so
                # it is the only place a status transition can be observed.
                # Guarded rather than always-called: most vendors publish no
                # status at all, and those syncs should not pay for a write
                # they cannot inform.
                if vendor_matter_status:
                    await self.db.matters.sync_vendor_status(
                        agenda_item.matter_id,
                        vendor_matter_status,
                        conn=conn,
                    )
                if (
                    not any(
                        item.meeting_id == meeting.id
                        for item in snapshot.prior_appearances.get(
                            agenda_item.matter_id, []
                        )
                    )
                    and (agenda_item.matter_file or raw_vendor_matter_id)
                ):
                    logger.info(
                        "matter new appearance",
                        matter=agenda_item.matter_file or raw_vendor_matter_id,
                        matter_type=matter_type,
                    )

                # Procedural filtering and enqueue/copy decisions happen only
                # after authoritative appearances are re-read below.
                if is_procedural:
                    continue
            else:
                if (
                    not agenda_item.matter_file
                    and not raw_vendor_matter_id
                    and not agenda_item.title
                ):
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
                    # This insert only establishes the aggregate/FK. The
                    # post-write publication phase immediately replaces this
                    # proposed seed with the complete retained appearance set.
                    attachments=list(
                        MatterWorkSnapshot.from_appearances([agenda_item]).attachments
                    ),
                    metadata=MatterMetadata(),
                    first_seen=meeting.date,
                    last_seen=meeting.date,
                    appearance_count=1,
                    status=vendor_matter_status or "active",
                )

                await self.db.matters.store_matter(matter_obj, conn=conn)
                stats["tracked"] += 1

                if agenda_item.matter_file or raw_vendor_matter_id:
                    logger.info(
                        "new matter tracked",
                        matter=agenda_item.matter_file or raw_vendor_matter_id,
                        matter_type=matter_type,
                        sponsor_count=len(sponsors),
                    )

                # Skip sponsor/vote work for procedural matters, as before.
                if is_procedural:
                    continue

            votes = raw_item.get("votes", [])
            if votes:
                result = self.vote_processor.process_votes(votes)
                stats["appearance_outcomes"][agenda_item.id] = {
                    "matter_id": agenda_item.matter_id,
                    "vote_outcome": result["outcome"],
                    "vote_tally": result["tally"],
                }

        return stats

    async def _reconcile_matter_appearances(
        self,
        meeting: Meeting,
        appearance_outcomes: Mapping[str, Mapping[str, Any]],
        *,
        affected_matter_ids: Optional[set[str]] = None,
        observed_votes: Optional[Mapping[str, List[Dict[str, Any]]]] = None,
        conn: Connection,
    ) -> Dict[str, int]:
        """Reconcile retained relationships, then attach vote outcomes.

        Outcome writes intentionally follow relationship creation. This makes
        the first observed vote for a brand-new appearance durable instead of
        issuing an UPDATE against a row that does not exist yet.
        """
        committee = meeting.title.split("-")[0].strip() if meeting.title else None
        changes = await self.db.matters.reconcile_meeting_appearances(
            meeting_id=meeting.id,
            appeared_at=meeting.date,
            committee=committee,
            committee_id=getattr(meeting, "committee_id", None),
            conn=conn,
        )
        if affected_matter_ids:
            ordered_matter_ids = sorted(affected_matter_ids)
            await self.db.council_members.reconcile_matter_sponsorships(
                banana=meeting.banana,
                affected_matter_ids=ordered_matter_ids,
                conn=conn,
            )
            await self.db.council_members.reconcile_meeting_votes(
                banana=meeting.banana,
                meeting_id=meeting.id,
                affected_matter_ids=ordered_matter_ids,
                observed_votes=observed_votes or {},
                vote_date=meeting.date,
                conn=conn,
            )
        for item_id in sorted(appearance_outcomes):
            outcome = appearance_outcomes[item_id]
            await self.db.matters.update_appearance_outcome(
                matter_id=outcome["matter_id"],
                meeting_id=meeting.id,
                item_id=item_id,
                vote_outcome=outcome["vote_outcome"],
                vote_tally=outcome["vote_tally"],
                conn=conn,
            )
        return changes

    async def _publish_authoritative_work(
        self,
        *,
        meeting_id: str,
        affected_matter_ids: set[str],
        procedural_matter_ids: set[str],
        conn: Connection,
        publish_meeting: bool,
        chunk_audit: Optional[Dict[str, Any]] = None,
        html_audit: Optional[Dict[str, Any]] = None,
    ) -> Meeting:
        """Derive desired work from locked rows retained by the database.

        Store methods intentionally preserve some existing values through
        ``COALESCE`` and freeze summarized item fields during UPSERT. Proposed
        adapter objects therefore cannot be publication truth. This method is
        the single post-write boundary for both meeting and matter work:

        1. lock and re-read the meeting aggregate;
        2. lock affected matter aggregates in stable order;
        3. lock and re-read their complete item appearances;
        4. apply unchanged-copy/tracking decisions and publish exact versions;
        5. re-read meeting items after any copy, then publish meeting work.

        The ordering preserves the system-wide domain -> items lock hierarchy.
        """
        authoritative_meeting = await self.db.meetings.get_meeting(
            meeting_id,
            conn=conn,
            lock_for_update=True,
        )
        if authoritative_meeting is None:
            raise DatabaseError(
                f"meeting disappeared during authoritative publication: {meeting_id}"
            )

        ordered_matter_ids = sorted(affected_matter_ids)
        authoritative_matters = (
            await self.db.matters.get_matters_for_sync_snapshot(
                ordered_matter_ids,
                conn=conn,
                include_unsummarized_orphans=True,
            )
            if ordered_matter_ids
            else {}
        )
        authoritative_appearances = (
            await self.db.items.get_all_items_for_matters(
                ordered_matter_ids,
                conn=conn,
                lock_for_update=True,
            )
            if ordered_matter_ids
            else {}
        )
        authoritative_tracking = (
            await self.db.matters.get_authoritative_tracking_for_matters(
                ordered_matter_ids,
                conn=conn,
            )
            if ordered_matter_ids
            else {}
        )

        matter_publications: List[Dict[str, Any]] = []
        for matter_id in ordered_matter_ids:
            matter = authoritative_matters.get(matter_id)
            if matter is None:
                raise DatabaseError(
                    f"matter disappeared during authoritative publication: {matter_id}"
                )

            appearances = authoritative_appearances.get(matter_id, [])
            matter_work = MatterWorkSnapshot.from_appearances(appearances)
            representative = (
                matter_work.appearances[0] if matter_work.appearances else None
            )
            retained_title = (
                str(getattr(representative, "title", "") or "")
                if representative is not None
                # With no retained source, keep the stable identity label;
                # all derived content is cleared below in the repository.
                else matter.title
            )
            retained_sponsors: List[str] = []
            seen_sponsors: set[str] = set()
            for appearance in matter_work.appearances:
                raw_sponsors = getattr(appearance, "sponsors", None) or []
                sponsor_names = (
                    [raw_sponsors]
                    if isinstance(raw_sponsors, str)
                    else raw_sponsors
                )
                if not isinstance(sponsor_names, list):
                    continue
                for sponsor in sponsor_names:
                    if not isinstance(sponsor, str):
                        continue
                    display_name = str(sponsor).strip()
                    normalized_name = normalize_sponsor_name(display_name)
                    if display_name and normalized_name not in seen_sponsors:
                        seen_sponsors.add(normalized_name)
                        retained_sponsors.append(display_name)
            current_appearances = [
                item for item in appearances if item.meeting_id == meeting_id
            ]
            current_title = (
                current_appearances[0].title
                if current_appearances
                else retained_title
            )

            should_enqueue = False
            skip_reason: Optional[str] = None
            if matter_id not in procedural_matter_ids:
                should_enqueue, skip_reason = (
                    self.matter_enqueue_decider.should_enqueue_matter(
                        existing_matter=matter,
                        current_attachment_hash=matter_work.attachment_version,
                        has_attachments=matter_work.is_summarizable,
                        current_attachment_hash_legacy=(
                            matter_work.legacy_attachment_version
                        ),
                        current_work_version=matter_work.work_version,
                        current_title=current_title,
                    )
                )

            confirmed_unchanged = (
                bool(skip_reason)
                and not should_enqueue
                and (skip_reason != "no_attachments")
            )
            tracking = authoritative_tracking[matter_id]
            await self.db.matters.refresh_matter_tracking(
                matter_id=matter_id,
                attachments=list(matter_work.attachments),
                appearance_count=tracking["appearance_count"],
                first_seen=tracking["first_seen"],
                last_seen=tracking["last_seen"],
                sponsors=retained_sponsors,
                title=retained_title,
                attachment_hash=(
                    matter_work.attachment_version if confirmed_unchanged else None
                ),
                work_version=(
                    matter_work.work_version if confirmed_unchanged else None
                ),
                conn=conn,
            )

            if should_enqueue:
                matter_publications.append(
                    {
                        "kind": "enqueue",
                        "matter_id": matter_id,
                        "work_version": matter_work.work_version,
                        "banana": authoritative_meeting.banana,
                        "meeting_date": authoritative_meeting.date,
                    }
                )
            elif matter_id in procedural_matter_ids or not matter_work.is_summarizable:
                # A relink can make the old aggregate empty. Publish an exact
                # terminal descriptor so older queue/outbox work cannot
                # resurrect after the authoritative A -> B transaction.
                no_work_reason: MatterNoWorkReason
                if matter_id in procedural_matter_ids:
                    no_work_reason = "procedural"
                elif not matter_work.appearances:
                    no_work_reason = "no_appearances"
                else:
                    no_work_reason = "no_substantive_work"
                matter_publications.append(
                    {
                        "kind": "tombstone",
                        "matter_id": matter_id,
                        "work_version": matter_no_work_version(
                            matter_work.work_version,
                            no_work_reason,
                        ),
                        "no_work_reason": no_work_reason,
                        "banana": authoritative_meeting.banana,
                    }
                )
            elif skip_reason == "attachments_unchanged":
                for item in current_appearances:
                    copied = await self.db.items.copy_summary_from_prior_appearance(
                        matter_id=matter_id,
                        target_item_id=item.id,
                        target_meeting_id=meeting_id,
                        conn=conn,
                    )
                    if copied:
                        logger.debug(
                            "reused prior-appearance summary (attachments unchanged)",
                            matter=matter_id,
                        )

        # Copying an unchanged matter may have completed an item. Re-read the
        # meeting's rows after that write so both the enqueue decision and its
        # version describe the final authoritative transaction state.
        authoritative_items = await self.db.items.get_agenda_items(
            meeting_id,
            conn=conn,
            lock_for_update=True,
        )

        # The combined action list retains sorted matter/source order for both
        # executable work and no-work tombstones.
        for publication in matter_publications:
            if publication["kind"] == "enqueue":
                await self._enqueue_matter_job(
                    matter_id=publication["matter_id"],
                    work_version=publication["work_version"],
                    banana=publication["banana"],
                    meeting_date=publication["meeting_date"],
                    conn=conn,
                )
            else:
                matter_id = publication["matter_id"]
                await self.db.queue.invalidate_desired_work(
                    f"matter://{matter_id}",
                    "matter",
                    {
                        "matter_id": matter_id,
                        "no_work_reason": publication["no_work_reason"],
                    },
                    work_version=publication["work_version"],
                    banana=publication["banana"],
                    conn=conn,
                )

        if publish_meeting:
            await self._enqueue_if_needed(
                authoritative_meeting,
                authoritative_meeting.date,
                authoritative_items,
                conn=conn,
                chunk_audit=chunk_audit,
                html_audit=html_audit,
            )

        return authoritative_meeting

    async def _enqueue_if_needed(
        self,
        stored_meeting: Meeting,
        meeting_date: Optional[datetime],
        agenda_items: List[AgendaItem],
        conn: Connection,
        chunk_audit: Optional[Dict[str, Any]] = None,
        html_audit: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Enqueue meeting for LLM processing if criteria are met."""
        should_enqueue, skip_reason = self.enqueue_decider.should_enqueue(
            stored_meeting, agenda_items, bool(agenda_items)
        )

        if not should_enqueue:
            if skip_reason:
                logger.debug(
                    "skipping enqueue", reason=skip_reason, meeting_id=stored_meeting.id
                )
            return

        priority = self.enqueue_decider.calculate_priority(meeting_date)

        work_version = meeting_work_version(stored_meeting, agenda_items)
        # Chunk diagnostics are retained across re-enqueues as sticky routing
        # history. Stamp the semantic audit with the exact domain inputs it
        # measured so processors never apply an old document shape to newer
        # HTML/API items that arrived without a fresh chunk run.
        versioned_chunk_audit = (
            {**chunk_audit, "work_version": work_version}
            if chunk_audit
            else None
        )
        await self.db.pipeline_lifecycle.enqueue_queue_job(
            source_url=f"meeting://{stored_meeting.id}",
            job_type="meeting",
            payload={"meeting_id": stored_meeting.id},
            aggregate_id=stored_meeting.id,
            meeting_id=stored_meeting.id,
            priority=priority,
            banana=stored_meeting.banana,
            work_version=work_version,
            processing_metadata=(
                {
                    k: v
                    for k, v in (
                        ("chunk", versioned_chunk_audit),
                        ("html", html_audit),
                    )
                    if v
                }
                or None
            ),
            conn=conn,
        )

        logger.info(
            "enqueued meeting for processing",
            meeting_id=stored_meeting.id,
            priority=priority,
        )

    async def _enqueue_matter_job(
        self,
        matter_id: str,
        work_version: str,
        banana: str,
        meeting_date: Optional[datetime],
        conn: Connection,
    ) -> None:
        priority = self.matter_enqueue_decider.calculate_priority(meeting_date)

        await self.db.pipeline_lifecycle.enqueue_queue_job(
            source_url=f"matter://{matter_id}",
            job_type="matter",
            payload={"matter_id": matter_id},
            aggregate_id=matter_id,
            meeting_id=None,
            banana=banana,
            priority=priority,
            work_version=work_version,
            conn=conn,
        )

        logger.info(
            "enqueued matter for processing", matter_id=matter_id, priority=priority
        )

    @staticmethod
    async def _claim_city_activation(banana: str, conn: Connection) -> bool:
        """Serialize and identify the no-meeting -> first-meeting transition."""
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            f"city-activation:{banana}",
        )
        return not bool(
            await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM meetings WHERE banana = $1)",
                banana,
            )
        )

    async def _enqueue_city_activation(
        self,
        city: Jurisdiction,
        conn: Connection,
    ) -> int:
        """Atomically record per-user activation deliveries with the first meeting."""
        recipients = await self.db.userland.get_city_activation_recipients(
            city.banana,
            conn=conn,
        )
        for recipient in recipients:
            await self.db.pipeline_lifecycle.enqueue_outbox(
                event_key=(
                    f"notification.city_activated:{city.banana}:{recipient['user_id']}"
                ),
                event_type="notification.city_activated",
                aggregate_type="jurisdiction",
                # Recipients have no causal ordering relationship. Give each
                # delivery its own FIFO aggregate so one bad address cannot
                # hold every other user behind its retry schedule.
                aggregate_id=f"{city.banana}:{recipient['user_id']}",
                payload={
                    "banana": city.banana,
                    "city_name": city.name,
                    "state": city.state,
                    "user_id": str(recipient["user_id"]),
                    "email": str(recipient["email"]),
                    "user_name": str(recipient["name"]),
                },
                conn=conn,
            )

        await self.db.userland.update_city_request_status(
            banana=city.banana,
            status="added",
            notes=f"First meeting synced {datetime.now().isoformat()}",
            conn=conn,
        )
        logger.info(
            "recorded city activation",
            banana=city.banana,
            notification_count=len(recipients),
        )
        return len(recipients)
