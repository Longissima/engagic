#!/usr/bin/env python3
"""
PostgreSQL Edge Case Tests

Tests edge cases, NULL handling, JSONB validation, and matter deduplication
to ensure production robustness.

Run with:
    ENGAGIC_USE_POSTGRES=true python tests/test_postgres_edge_cases.py
"""

import asyncio
import sys
import os
from datetime import datetime

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.db_postgres import Database
from database.models import Meeting, AgendaItem, Matter
from config import config, get_logger

logger = get_logger(__name__)


class EdgeCaseTests:
    """Edge case test suite for PostgreSQL migration"""

    def __init__(self, db: Database):
        self.db = db
        self.passed = 0
        self.failed = 0

    def _log_test(self, test_name: str, passed: bool, message: str = ""):
        """Log test result"""
        if passed:
            self.passed += 1
            print(f"✅ PASS | {test_name}")
            if message:
                print(f"         {message}")
        else:
            self.failed += 1
            print(f"❌ FAIL | {test_name}")
            if message:
                print(f"         {message}")

    async def test_meeting_null_date(self):
        """Test meeting with NULL date"""
        test_name = "Meeting with NULL date"

        try:
            meeting = Meeting(
                id="test_null_date",
                banana="testCA",
                title="Test Meeting",
                date=None,
                source_url="https://test.com",
            )

            await self.db.meetings.store_meeting(meeting)
            retrieved = await self.db.meetings.get_meeting("test_null_date")

            if retrieved and retrieved.date is None:
                self._log_test(test_name, True, "NULL date handled correctly")
            else:
                self._log_test(test_name, False, f"Expected NULL date, got {retrieved.date if retrieved else 'None'}")

        except Exception as e:
            self._log_test(test_name, False, f"Exception: {e}")

    async def test_item_empty_attachments(self):
        """Test item with empty attachments list"""
        test_name = "Item with empty attachments"

        try:
            # First create a meeting
            meeting = Meeting(
                id="test_meeting_empty_attach",
                banana="testCA",
                title="Test Meeting",
                date=datetime.now(),
                source_url="https://test.com",
            )
            await self.db.meetings.store_meeting(meeting)

            item = AgendaItem(
                id="test_empty_attach",
                meeting_id="test_meeting_empty_attach",
                title="Test Item",
                attachments=[],  # Empty list
                sponsors=[],     # Empty list
            )

            await self.db.items.store_agenda_items("test_meeting_empty_attach", [item])
            retrieved = await self.db.items.get_agenda_item("test_empty_attach")

            if retrieved and retrieved.attachments == []:
                self._log_test(test_name, True, "Empty attachments handled correctly")
            else:
                self._log_test(test_name, False, f"Expected empty list, got {retrieved.attachments if retrieved else 'None'}")

        except Exception as e:
            self._log_test(test_name, False, f"Exception: {e}")

    async def test_item_null_fields(self):
        """Test item with NULL optional fields"""
        test_name = "Item with NULL optional fields"

        try:
            # Create meeting first
            meeting = Meeting(
                id="test_meeting_null_fields",
                banana="testCA",
                title="Test Meeting",
                date=datetime.now(),
                source_url="https://test.com",
            )
            await self.db.meetings.store_meeting(meeting)

            item = AgendaItem(
                id="test_null_fields",
                meeting_id="test_meeting_null_fields",
                title="Test Item",
                attachments=None,
                sponsors=None,
                matter_id=None,
                matter_file=None,
                summary=None,
                topics=None,
            )

            await self.db.items.store_agenda_items("test_meeting_null_fields", [item])
            retrieved = await self.db.items.get_agenda_item("test_null_fields")

            if retrieved:
                self._log_test(test_name, True, "NULL optional fields handled correctly")
            else:
                self._log_test(test_name, False, "Failed to retrieve item with NULL fields")

        except Exception as e:
            self._log_test(test_name, False, f"Exception: {e}")

    async def test_meeting_empty_topics(self):
        """Test meeting with empty topics list"""
        test_name = "Meeting with empty topics"

        try:
            meeting = Meeting(
                id="test_empty_topics",
                banana="testCA",
                title="Test Meeting",
                date=datetime.now(),
                source_url="https://test.com",
                topics=[],  # Empty topics
            )

            await self.db.meetings.store_meeting(meeting)
            retrieved = await self.db.meetings.get_meeting("test_empty_topics")

            # Check that no topic rows were created
            async with self.db.pool.acquire() as conn:
                topic_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM meeting_topics WHERE meeting_id = $1",
                    "test_empty_topics"
                )

            if retrieved and topic_count == 0:
                self._log_test(test_name, True, "Empty topics handled correctly")
            else:
                self._log_test(test_name, False, f"Expected 0 topics, found {topic_count}")

        except Exception as e:
            self._log_test(test_name, False, f"Exception: {e}")

    async def test_topic_deduplication(self):
        """Test that duplicate topics are deduplicated"""
        test_name = "Topic deduplication"

        try:
            meeting = Meeting(
                id="test_topic_dedup",
                banana="testCA",
                title="Test Meeting",
                date=datetime.now(),
                source_url="https://test.com",
                topics=["Housing", "Housing", "Zoning"],  # Duplicate "Housing"
            )

            await self.db.meetings.store_meeting(meeting)

            # Check topic count
            async with self.db.pool.acquire() as conn:
                topics = await conn.fetch(
                    "SELECT topic FROM meeting_topics WHERE meeting_id = $1 ORDER BY topic",
                    "test_topic_dedup"
                )

            topic_list = [row["topic"] for row in topics]

            # Should have 2 unique topics, not 3
            if len(topic_list) == 2 and topic_list == ["Housing", "Zoning"]:
                self._log_test(test_name, True, "Duplicate topics deduplicated correctly")
            else:
                self._log_test(test_name, False, f"Expected 2 unique topics, got {len(topic_list)}: {topic_list}")

        except Exception as e:
            self._log_test(test_name, False, f"Exception: {e}")

    async def test_matter_deduplication(self):
        """Test that same matter across meetings uses canonical summary"""
        test_name = "Matter deduplication"

        try:
            # Create two meetings
            meeting1 = Meeting(
                id="test_matter_meeting1",
                banana="testCA",
                title="Meeting 1",
                date=datetime.now(),
                source_url="https://test1.com",
            )
            meeting2 = Meeting(
                id="test_matter_meeting2",
                banana="testCA",
                title="Meeting 2",
                date=datetime.now(),
                source_url="https://test2.com",
            )

            await self.db.meetings.store_meeting(meeting1)
            await self.db.meetings.store_meeting(meeting2)

            # Create matter
            matter = Matter(
                id="test_matter_dedup",
                banana="testCA",
                matter_file="BL2025-TEST",
                title="Test Ordinance",
                canonical_summary="This is the canonical summary",
                canonical_topics=["Housing"],
            )
            await self.db.matters.store_matter(matter)

            # Create items in both meetings referencing same matter
            item1 = AgendaItem(
                id="test_matter_item1",
                meeting_id="test_matter_meeting1",
                title="First Reading - Test Ordinance",
                matter_id="test_matter_dedup",
                matter_file="BL2025-TEST",
            )
            item2 = AgendaItem(
                id="test_matter_item2",
                meeting_id="test_matter_meeting2",
                title="Second Reading - Test Ordinance",
                matter_id="test_matter_dedup",
                matter_file="BL2025-TEST",
            )

            await self.db.items.store_agenda_items("test_matter_meeting1", [item1])
            await self.db.items.store_agenda_items("test_matter_meeting2", [item2])

            # Verify matter appears in both meetings
            async with self.db.pool.acquire() as conn:
                appearance_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM matter_appearances WHERE matter_id = $1",
                    "test_matter_dedup"
                )

            if appearance_count == 2:
                self._log_test(test_name, True, "Matter tracked across 2 meetings")
            else:
                self._log_test(test_name, False, f"Expected 2 appearances, found {appearance_count}")

        except Exception as e:
            self._log_test(test_name, False, f"Exception: {e}")

    async def test_jsonb_structure_validation(self):
        """Test JSONB structure validation"""
        test_name = "JSONB structure validation"

        try:
            meeting = Meeting(
                id="test_jsonb",
                banana="testCA",
                title="Test Meeting",
                date=datetime.now(),
                source_url="https://test.com",
                participation={
                    "email": "public@test.com",
                    "phone": "+1-555-0100",
                    "zoom": "https://zoom.us/test",
                }
            )

            await self.db.meetings.store_meeting(meeting)
            retrieved = await self.db.meetings.get_meeting("test_jsonb")

            if (retrieved and
                isinstance(retrieved.participation, dict) and
                retrieved.participation.get("email") == "public@test.com"):
                self._log_test(test_name, True, "JSONB participation stored and retrieved correctly")
            else:
                self._log_test(test_name, False, f"JSONB structure invalid: {retrieved.participation if retrieved else 'None'}")

        except Exception as e:
            self._log_test(test_name, False, f"Exception: {e}")

    async def test_queue_duplicate_source_url(self):
        """Test that duplicate source_url updates existing job"""
        test_name = "Queue duplicate source_url"

        try:
            # Enqueue first job
            await self.db.queue.enqueue_job(
                source_url="https://test.com/duplicate",
                job_type="meeting",
                banana="testCA",
                payload={"test": "first"},
            )

            # Enqueue duplicate (should update, not create new)
            await self.db.queue.enqueue_job(
                source_url="https://test.com/duplicate",
                job_type="meeting",
                banana="testCA",
                payload={"test": "second"},
            )

            # Check queue count
            async with self.db.pool.acquire() as conn:
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM queue WHERE source_url = $1",
                    "https://test.com/duplicate"
                )

            if count == 1:
                self._log_test(test_name, True, "Duplicate source_url updates existing job")
            else:
                self._log_test(test_name, False, f"Expected 1 job, found {count}")

        except Exception as e:
            self._log_test(test_name, False, f"Exception: {e}")

    async def test_cross_city_matter_isolation(self):
        """Test that matters from different cities don't collide"""
        test_name = "Cross-city matter isolation"

        try:
            # Create same matter_file in two different cities
            matter1 = Matter(
                id="test_city1_matter",
                banana="city1CA",
                matter_file="BL2025-1",  # Same number
                title="City 1 Ordinance",
                canonical_summary="City 1 summary",
            )
            matter2 = Matter(
                id="test_city2_matter",
                banana="city2CA",
                matter_file="BL2025-1",  # Same number, different city
                title="City 2 Ordinance",
                canonical_summary="City 2 summary",
            )

            await self.db.matters.store_matter(matter1)
            await self.db.matters.store_matter(matter2)

            # Verify both exist
            retrieved1 = await self.db.matters.get_matter("test_city1_matter")
            retrieved2 = await self.db.matters.get_matter("test_city2_matter")

            if (retrieved1 and retrieved2 and
                retrieved1.banana == "city1CA" and
                retrieved2.banana == "city2CA"):
                self._log_test(test_name, True, "Cross-city matters isolated correctly")
            else:
                self._log_test(test_name, False, "Matter collision detected")

        except Exception as e:
            self._log_test(test_name, False, f"Exception: {e}")

    async def _seed_temporal_snapshot_fixture(self, banana: str):
        """Seed a jurisdiction + prior meeting + prior item with summary.
        Shared setup for the temporal-snapshot tests.
        Returns (matter_id, prior_item_id, prior_attachments, substantive_hash).
        """
        from database.id_generation import generate_matter_id
        from database.models import Matter, MatterMetadata, AttachmentInfo
        from pipeline.utils import hash_substantive_attachments

        async with self.db.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO jurisdictions (banana, name, state, vendor, slug) "
                "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (banana) DO NOTHING",
                banana, "Test Snapshot City", "CA", "granicus", f"{banana}-slug"
            )

        prior_meeting_id = f"test_snap_meeting_a_{banana}"
        prior_meeting = Meeting(
            id=prior_meeting_id,
            banana=banana,
            title="Prior Meeting",
            date=datetime(2026, 1, 1),
        )
        await self.db.meetings.store_meeting(prior_meeting)

        prior_attachments = [
            AttachmentInfo(name="ordinance_v1.pdf", url="https://test.com/ord1.pdf", type="pdf")
        ]
        substantive_hash = hash_substantive_attachments(prior_attachments)

        matter_id = generate_matter_id(banana=banana, matter_file="ORD-2026-SNAP")
        assert matter_id is not None, "generate_matter_id returned None for valid matter_file"
        matter = Matter(
            id=matter_id,
            banana=banana,
            matter_file="ORD-2026-SNAP",
            title="Snapshot Test Ordinance",
            canonical_summary="Original canonical summary from matter job",
            canonical_topics=["housing"],
            attachments=prior_attachments,
            metadata=MatterMetadata(attachment_hash=substantive_hash),
            first_seen=datetime(2026, 1, 1),
            last_seen=datetime(2026, 1, 1),
            appearance_count=1,
        )
        await self.db.matters.store_matter(matter)

        prior_item_id = f"test_snap_item_a_{banana}"
        prior_item = AgendaItem(
            id=prior_item_id,
            meeting_id=prior_meeting_id,
            title="First Reading - Snapshot Ordinance",
            sequence=1,
            matter_id=matter_id,
            matter_file="ORD-2026-SNAP",
            attachments=prior_attachments,
            summary="First-reading frozen snapshot summary",
            topics=["housing"],
        )
        await self.db.items.store_agenda_items(prior_meeting_id, [prior_item])

        return matter_id, prior_item_id, prior_attachments, substantive_hash

    async def test_copy_summary_unit_returns_true_on_success(self):
        """Test A1: copy_summary_from_prior_appearance copies summary when target row exists with NULL."""
        test_name = "copy_summary unit: success returns True"
        banana = "testsnapaCA"
        try:
            matter_id, _, prior_attachments, _ = await self._seed_temporal_snapshot_fixture(banana)

            target_meeting_id = f"test_snap_meeting_b_{banana}"
            target_meeting = Meeting(
                id=target_meeting_id,
                banana=banana,
                title="Target Meeting",
                date=datetime(2026, 2, 1),
            )
            await self.db.meetings.store_meeting(target_meeting)

            target_item_id = f"test_snap_item_b_{banana}"
            target_item = AgendaItem(
                id=target_item_id,
                meeting_id=target_meeting_id,
                title="Second Reading - Snapshot Ordinance",
                sequence=1,
                matter_id=matter_id,
                matter_file="ORD-2026-SNAP",
                attachments=prior_attachments,
                summary=None,  # the row we expect to be filled
                topics=None,
            )
            await self.db.items.store_agenda_items(target_meeting_id, [target_item])

            copied = await self.db.items.copy_summary_from_prior_appearance(
                matter_id=matter_id,
                target_item_id=target_item_id,
                target_meeting_id=target_meeting_id,
            )

            retrieved = await self.db.items.get_agenda_item(target_item_id)
            if copied and retrieved and retrieved.summary == "First-reading frozen snapshot summary":
                self._log_test(test_name, True, "Summary copied onto target item")
            else:
                self._log_test(test_name, False, f"copied={copied}, summary={retrieved.summary if retrieved else 'None'}")
        except Exception as e:
            self._log_test(test_name, False, f"Exception: {e}")

    async def test_copy_summary_unit_idempotent_returns_false(self):
        """Test A2: Second call on an already-filled row returns False (WHERE summary IS NULL)."""
        test_name = "copy_summary unit: idempotent returns False"
        banana = "testsnapbCA"
        try:
            matter_id, _, prior_attachments, _ = await self._seed_temporal_snapshot_fixture(banana)

            target_meeting_id = f"test_snap_meeting_b_{banana}"
            await self.db.meetings.store_meeting(Meeting(
                id=target_meeting_id, banana=banana, title="Target", date=datetime(2026, 2, 1),
            ))
            target_item_id = f"test_snap_item_b_{banana}"
            await self.db.items.store_agenda_items(target_meeting_id, [AgendaItem(
                id=target_item_id, meeting_id=target_meeting_id, title="Second Reading",
                sequence=1, matter_id=matter_id, matter_file="ORD-2026-SNAP",
                attachments=prior_attachments, summary=None, topics=None,
            )])

            first = await self.db.items.copy_summary_from_prior_appearance(
                matter_id=matter_id, target_item_id=target_item_id, target_meeting_id=target_meeting_id,
            )
            second = await self.db.items.copy_summary_from_prior_appearance(
                matter_id=matter_id, target_item_id=target_item_id, target_meeting_id=target_meeting_id,
            )

            if first is True and second is False:
                self._log_test(test_name, True, "Second call correctly returned False")
            else:
                self._log_test(test_name, False, f"first={first}, second={second} (expected True, False)")
        except Exception as e:
            self._log_test(test_name, False, f"Exception: {e}")

    async def test_copy_summary_unit_missing_target_returns_false(self):
        """Test A3: Nonexistent target_item_id returns False without FK violation.
        This is the scenario that the Fix 1 bug triggered in production."""
        test_name = "copy_summary unit: missing target returns False"
        banana = "testsnapcCA"
        try:
            matter_id, _, _, _ = await self._seed_temporal_snapshot_fixture(banana)

            # Target meeting exists but target item does NOT -- mimics pre-Fix-1
            # ordering where copy runs before store_agenda_items.
            target_meeting_id = f"test_snap_meeting_b_{banana}"
            await self.db.meetings.store_meeting(Meeting(
                id=target_meeting_id, banana=banana, title="Target", date=datetime(2026, 2, 1),
            ))

            result = await self.db.items.copy_summary_from_prior_appearance(
                matter_id=matter_id,
                target_item_id=f"test_snap_nonexistent_{banana}",
                target_meeting_id=target_meeting_id,
            )

            # Must return False cleanly, NOT raise FK violation on item_topics insert
            if result is False:
                self._log_test(test_name, True, "Missing target returned False without FK error")
            else:
                self._log_test(test_name, False, f"Expected False, got {result}")
        except Exception as e:
            # If this raises a FK violation, Fix 2 is broken
            self._log_test(test_name, False, f"Raised exception (Fix 2 regression?): {e}")

    async def test_copy_summary_unit_no_prior_returns_false(self):
        """Test A4: If no prior appearance exists, returns False without writing anything."""
        test_name = "copy_summary unit: no prior returns False"
        banana = "testsnapdCA"
        try:
            async with self.db.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO jurisdictions (banana, name, state, vendor, slug) "
                    "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (banana) DO NOTHING",
                    banana, "Test D", "CA", "granicus", f"{banana}-slug"
                )

            from database.id_generation import generate_matter_id
            from database.models import MatterMetadata
            matter_id = generate_matter_id(banana=banana, matter_file="ORD-LONELY")
            assert matter_id is not None
            # Matter exists but no prior summarized item
            await self.db.matters.store_matter(Matter(
                id=matter_id, banana=banana, matter_file="ORD-LONELY", title="Lonely",
                metadata=MatterMetadata(attachment_hash="deadbeef"),
            ))

            target_meeting_id = f"test_snap_meeting_d_{banana}"
            await self.db.meetings.store_meeting(Meeting(
                id=target_meeting_id, banana=banana, title="Target", date=datetime(2026, 2, 1),
            ))
            target_item_id = f"test_snap_item_d_{banana}"
            await self.db.items.store_agenda_items(target_meeting_id, [AgendaItem(
                id=target_item_id, meeting_id=target_meeting_id, title="First Appearance",
                sequence=1, matter_id=matter_id, matter_file="ORD-LONELY",
                attachments=[], summary=None, topics=None,
            )])

            result = await self.db.items.copy_summary_from_prior_appearance(
                matter_id=matter_id, target_item_id=target_item_id, target_meeting_id=target_meeting_id,
            )

            retrieved = await self.db.items.get_agenda_item(target_item_id)
            if result is False and retrieved and retrieved.summary is None:
                self._log_test(test_name, True, "No prior found, returned False without modification")
            else:
                self._log_test(test_name, False, f"result={result}, summary={retrieved.summary if retrieved else 'None'}")
        except Exception as e:
            self._log_test(test_name, False, f"Exception: {e}")

    async def test_track_matters_flow_unchanged_attachments(self):
        """Test B: Full transaction flow with unchanged substantive attachments.
        This is the test that would have caught the Fix 1 ordering bug.
        Simulates the current post-write authoritative publication boundary and
        asserts the retained target row receives the prior summary without a
        redundant matter publication."""
        test_name = "Flow test: unchanged attachments -> copy summary"
        banana = "testsnapeCA"
        try:
            from pipeline.orchestrators.meeting_sync import MeetingSyncOrchestrator

            matter_id, _, prior_attachments, _ = await self._seed_temporal_snapshot_fixture(banana)

            orchestrator = MeetingSyncOrchestrator(self.db)

            target_meeting_id = f"test_snap_meeting_b_{banana}"
            target_meeting = Meeting(
                id=target_meeting_id,
                banana=banana,
                title="Target Meeting",
                date=datetime(2026, 2, 1),
            )
            target_item_id = f"test_snap_item_b_{banana}"
            target_item = AgendaItem(
                id=target_item_id,
                meeting_id=target_meeting_id,
                title="Second Reading - Snapshot Ordinance",
                sequence=1,
                matter_id=matter_id,
                matter_file="ORD-2026-SNAP",
                attachments=prior_attachments,  # Same as prior -> hash matches
                summary=None,
                topics=None,
            )

            items_data = [{
                "sequence": 1,
                "matter_file": "ORD-2026-SNAP",
                "matter_id": None,
                "matter_type": None,
                "sponsors": [],
                "votes": [],
            }]

            # Execute the exact transaction flow that sync_meeting performs.
            async with self.db.pool.acquire() as conn:
                async with conn.transaction():
                    await self.db.meetings.store_meeting(target_meeting, conn=conn)
                    pre_links = await self.db.items.get_item_matter_links(
                        target_meeting_id, conn=conn
                    )
                    affected_matter_ids = orchestrator._affected_matter_ids(
                        pre_links, [target_item]
                    )
                    matters_stats = await orchestrator._track_matters(
                        target_meeting,
                        items_data,
                        [target_item],
                        affected_matter_ids=affected_matter_ids,
                        conn=conn,
                    )
                    await self.db.items.store_agenda_items(
                        target_meeting_id, [target_item], conn=conn
                    )
                    await orchestrator._reconcile_matter_appearances(
                        target_meeting,
                        matters_stats.get("appearance_outcomes", {}),
                        affected_matter_ids=affected_matter_ids,
                        observed_votes=matters_stats.get("observed_votes", {}),
                        conn=conn,
                    )
                    await orchestrator._publish_authoritative_work(
                        meeting_id=target_meeting_id,
                        affected_matter_ids=set(
                            matters_stats.get("affected_matter_ids", set())
                        ),
                        procedural_matter_ids=set(
                            matters_stats.get("procedural_matter_ids", set())
                        ),
                        conn=conn,
                        publish_meeting=False,
                    )
                    matter_publications = await conn.fetchval(
                        """
                        SELECT COUNT(*)
                        FROM pipeline_outbox
                        WHERE event_type = 'queue.enqueue'
                          AND payload->>'source_url' = $1
                        """,
                        f"matter://{matter_id}",
                    )

            retrieved = await self.db.items.get_agenda_item(target_item_id)

            # Assertions:
            # 1. Transaction did not roll back (we got here without exception)
            # 2. Target item now carries the prior appearance's summary
            # 3. No new matter publication was recorded
            checks = [
                (retrieved is not None, "target item exists"),
                (retrieved and retrieved.summary == "First-reading frozen snapshot summary", "summary copied from prior"),
                (matter_publications == 0, "matter publication suppressed"),
            ]
            failed = [msg for ok, msg in checks if not ok]
            if not failed:
                self._log_test(test_name, True, "Ordering fix works: summary copied in-transaction, no LLM enqueue")
            else:
                self._log_test(test_name, False, f"Failed checks: {failed}; got summary={retrieved.summary if retrieved else 'None'}")
        except Exception as e:
            self._log_test(test_name, False, f"Exception (likely FK violation from pre-Fix-1 ordering): {e}")

    async def test_track_matters_flow_changed_attachments(self):
        """Test C: Full transaction flow with changed substantive attachments.
        Asserts that the enqueue path fires (not the copy path) when attachments differ."""
        test_name = "Flow test: changed attachments -> enqueue matter job"
        banana = "testsnapfCA"
        try:
            from pipeline.orchestrators.meeting_sync import MeetingSyncOrchestrator
            from database.models import AttachmentInfo

            matter_id, _, _, _ = await self._seed_temporal_snapshot_fixture(banana)

            orchestrator = MeetingSyncOrchestrator(self.db)

            target_meeting_id = f"test_snap_meeting_b_{banana}"
            target_meeting = Meeting(
                id=target_meeting_id,
                banana=banana,
                title="Target Meeting",
                date=datetime(2026, 2, 1),
            )
            # Different attachment -> different substantive hash
            changed_attachments = [
                AttachmentInfo(name="ordinance_v2_amended.pdf", url="https://test.com/ord2.pdf", type="pdf")
            ]
            target_item_id = f"test_snap_item_b_{banana}"
            target_item = AgendaItem(
                id=target_item_id,
                meeting_id=target_meeting_id,
                title="Second Reading (Amended)",
                sequence=1,
                matter_id=matter_id,
                matter_file="ORD-2026-SNAP",
                attachments=changed_attachments,
                summary=None,
                topics=None,
            )
            items_data = [{
                "sequence": 1,
                "matter_file": "ORD-2026-SNAP",
                "matter_id": None,
                "matter_type": None,
                "sponsors": [],
                "votes": [],
            }]

            async with self.db.pool.acquire() as conn:
                async with conn.transaction():
                    await self.db.meetings.store_meeting(target_meeting, conn=conn)
                    pre_links = await self.db.items.get_item_matter_links(
                        target_meeting_id, conn=conn
                    )
                    affected_matter_ids = orchestrator._affected_matter_ids(
                        pre_links, [target_item]
                    )
                    matters_stats = await orchestrator._track_matters(
                        target_meeting,
                        items_data,
                        [target_item],
                        affected_matter_ids=affected_matter_ids,
                        conn=conn,
                    )
                    await self.db.items.store_agenda_items(
                        target_meeting_id, [target_item], conn=conn
                    )
                    await orchestrator._reconcile_matter_appearances(
                        target_meeting,
                        matters_stats.get("appearance_outcomes", {}),
                        affected_matter_ids=affected_matter_ids,
                        observed_votes=matters_stats.get("observed_votes", {}),
                        conn=conn,
                    )
                    await orchestrator._publish_authoritative_work(
                        meeting_id=target_meeting_id,
                        affected_matter_ids=set(
                            matters_stats.get("affected_matter_ids", set())
                        ),
                        procedural_matter_ids=set(
                            matters_stats.get("procedural_matter_ids", set())
                        ),
                        conn=conn,
                        publish_meeting=False,
                    )
                    matter_publications = await conn.fetchval(
                        """
                        SELECT COUNT(*)
                        FROM pipeline_outbox
                        WHERE event_type = 'queue.enqueue'
                          AND payload->>'source_url' = $1
                        """,
                        f"matter://{matter_id}",
                    )

            retrieved = await self.db.items.get_agenda_item(target_item_id)

            checks = [
                (matter_publications == 1, "one matter publication recorded"),
                (retrieved is not None, "target item exists"),
                (retrieved and retrieved.summary is None, "summary left NULL for matter job to fill"),
            ]
            failed = [msg for ok, msg in checks if not ok]
            if not failed:
                self._log_test(test_name, True, "Enqueue path fires on changed hash, item left unsummarized")
            else:
                self._log_test(test_name, False, f"Failed checks: {failed}")
        except Exception as e:
            self._log_test(test_name, False, f"Exception: {e}")

    async def cleanup(self):
        """Clean up test data"""
        try:
            async with self.db.pool.acquire() as conn:
                # Delete test data (cascades via foreign keys)
                await conn.execute("DELETE FROM meetings WHERE id LIKE 'test_%'")
                await conn.execute("DELETE FROM jurisdictions WHERE banana LIKE 'test%' OR banana LIKE 'city%'")
                await conn.execute("DELETE FROM queue WHERE source_url LIKE 'https://test.com%'")
                await conn.execute(
                    "DELETE FROM pipeline_outbox WHERE aggregate_id LIKE 'testsnap%'"
                )
                await conn.execute("DELETE FROM city_matters WHERE id LIKE 'test_%' OR banana LIKE 'testsnap%'")
            logger.info("cleaned up test data")
        except Exception as e:
            logger.warning("cleanup failed", error=str(e))

    async def run_all(self):
        """Run all edge case tests"""
        print("\n" + "="*80)
        print("PostgreSQL Edge Case Tests")
        print("="*80 + "\n")

        # Run tests
        await self.test_meeting_null_date()
        await self.test_item_empty_attachments()
        await self.test_item_null_fields()
        await self.test_meeting_empty_topics()
        await self.test_topic_deduplication()
        await self.test_matter_deduplication()
        await self.test_jsonb_structure_validation()
        await self.test_queue_duplicate_source_url()
        await self.test_cross_city_matter_isolation()

        # Temporal snapshot regression tests (2026-04-10 ordering fix)
        await self.test_copy_summary_unit_returns_true_on_success()
        await self.test_copy_summary_unit_idempotent_returns_false()
        await self.test_copy_summary_unit_missing_target_returns_false()
        await self.test_copy_summary_unit_no_prior_returns_false()
        await self.test_track_matters_flow_unchanged_attachments()
        await self.test_track_matters_flow_changed_attachments()

        # Cleanup
        await self.cleanup()

        # Summary
        print("\n" + "="*80)
        total = self.passed + self.failed
        print(f"Results: {self.passed}/{total} passed")
        print("="*80 + "\n")

        return self.failed == 0


async def main():
    """Main test entry point"""
    # Verify PostgreSQL is enabled
    if not config.USE_POSTGRES:
        print("❌ ENGAGIC_USE_POSTGRES is not set to 'true'")
        print("Set environment variable: export ENGAGIC_USE_POSTGRES=true")
        sys.exit(1)

    # Initialize database
    db = await Database.create()

    try:
        tests = EdgeCaseTests(db)
        passed = await tests.run_all()

        if passed:
            print("✅ ALL TESTS PASSED")
            sys.exit(0)
        else:
            print("❌ SOME TESTS FAILED")
            sys.exit(1)

    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
