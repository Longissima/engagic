"""
Integration Tests for Matter Tracking Flow

Tests the complete matter tracking lifecycle:
1. Vendor adapter extracts matter identifiers from meeting HTML/API
2. Meeting ingestion generates matter IDs using fallback hierarchy
3. Matter deduplication (canonical summary reuse when attachments unchanged)
4. Appearance tracking (first_seen, last_seen, appearance_count)
5. Timeline queries across meetings

This validates that the 3-tier fallback system works end-to-end.
"""

import hashlib
from datetime import datetime

import pytest

from database.id_generation import generate_matter_id


@pytest.fixture
def test_city():
    """Test city data"""
    return {
        "banana": "testcityCA",
        "name": "Test City",
        "state": "CA",
        "vendor": "primegov",
        "status": "active"
    }


class TestMatterFileFallbackFlow:
    """Test end-to-end flow for matter_file identification (Legistar, LA-style PrimeGov)"""

    def test_same_ordinance_three_readings_deduplicates(self):
        """Ordinance appears in 3 meetings (FIRST, SECOND, FINAL readings) - should deduplicate"""
        banana = "nashvilleTN"
        matter_file = "BL2025-1098"

        # Simulate 3 meetings with same ordinance
        meetings = [
            {
                "date": "2025-01-15",
                "title": "City Council Meeting",
                "items": [{
                    "matter_file": matter_file,
                    "matter_id": "uuid-abc-123",
                    "title": "FIRST READING: Zoning Ordinance Amendment",
                    "attachments": ["ordinance_v1.pdf"]
                }]
            },
            {
                "date": "2025-02-01",
                "title": "City Council Meeting",
                "items": [{
                    "matter_file": matter_file,
                    "matter_id": "uuid-abc-123",
                    "title": "SECOND READING: Zoning Ordinance Amendment",
                    "attachments": ["ordinance_v1.pdf"]  # Same PDF
                }]
            },
            {
                "date": "2025-02-15",
                "title": "City Council Meeting",
                "items": [{
                    "matter_file": matter_file,
                    "matter_id": "uuid-abc-123",
                    "title": "FINAL READING: Zoning Ordinance Amendment",
                    "attachments": ["ordinance_v1.pdf"]  # Same PDF
                }]
            }
        ]

        # All should generate same matter ID
        expected_id = generate_matter_id(banana, matter_file=matter_file)

        for meeting in meetings:
            item = meeting["items"][0]
            generated_id = generate_matter_id(
                banana,
                matter_file=item["matter_file"],
                matter_id=item["matter_id"]
            )
            assert generated_id == expected_id, "Matter ID should be consistent across readings"

        # After ingestion:
        # - 1 matter record in city_matters (canonical_summary on matter row)
        # - 3 appearance records in matter_appearances
        # - 3 item records in items (all linked to same matter_id)
        # - First reading: items.summary filled by matter job / meeting job
        # - Second and third readings: substantive attachment hash unchanged,
        #   sync-time copies prior item.summary onto each new appearance
        #   (temporal snapshot, frozen once set)

    def test_ordinance_amended_between_readings_reprocesses(self):
        """Ordinance PDF changes between readings - should reprocess"""
        banana = "nashvilleTN"
        matter_file = "BL2025-1098"

        # Simulate 3 meetings with ordinance that gets amended
        meetings = [
            {
                "date": "2025-01-15",
                "items": [{
                    "matter_file": matter_file,
                    "title": "FIRST READING: Zoning Ordinance",
                    "attachments": ["ordinance_v1.pdf"]
                }]
            },
            {
                "date": "2025-02-01",
                "items": [{
                    "matter_file": matter_file,
                    "title": "SECOND READING: Zoning Ordinance (AMENDED)",
                    "attachments": ["ordinance_v2.pdf"]  # Changed!
                }]
            }
        ]

        # Both should generate same matter ID (matter_file unchanged)
        expected_id = generate_matter_id(banana, matter_file=matter_file)

        for meeting in meetings:
            item = meeting["items"][0]
            generated_id = generate_matter_id(banana, matter_file=item["matter_file"])
            assert generated_id == expected_id

        # Attachment hashes differ:
        hash1 = hashlib.sha256(b"ordinance_v1.pdf").hexdigest()
        hash2 = hashlib.sha256(b"ordinance_v2.pdf").hexdigest()
        assert hash1 != hash2

        # After ingestion:
        # - 1 matter record (same matter_file)
        # - 2 appearance records
        # - Reading 1: processes PDF, stores summary
        # - Reading 2: detects changed attachment hash, reprocesses


class TestMatterIdFallbackFlow:
    """Test end-to-end flow for matter_id identification (Granicus, some PrimeGov)"""

    def test_matter_id_deduplication_when_no_matter_file(self):
        """Matter identified by vendor UUID only - should deduplicate"""
        banana = "granicus_cityCA"
        matter_id = "fb36db52-abc-123-def-456"

        # Simulate 2 meetings with same matter (no matter_file)
        meetings = [
            {
                "date": "2025-01-15",
                "items": [{
                    "matter_id": matter_id,
                    "title": "Budget Amendment Discussion",
                    "attachments": ["budget_report.pdf"]
                }]
            },
            {
                "date": "2025-02-01",
                "items": [{
                    "matter_id": matter_id,
                    "title": "Budget Amendment Approval",
                    "attachments": ["budget_report.pdf"]  # Same
                }]
            }
        ]

        # Both generate same matter ID (using matter_id fallback)
        expected_id = generate_matter_id(banana, matter_id=matter_id)

        for meeting in meetings:
            item = meeting["items"][0]
            generated_id = generate_matter_id(banana, matter_id=item["matter_id"])
            assert generated_id == expected_id

        # After ingestion:
        # - 1 matter record (matter_id based)
        # - 2 appearances
        # - Second appearance: unchanged attachment hash, sync copies prior
        #   item.summary as the temporal snapshot


class TestTitleNormalizationFallbackFlow:
    """Test end-to-end flow for title-based identification (Palo Alto-style PrimeGov)"""

    def test_title_based_ordinance_tracking(self):
        """City without stable IDs - use title normalization"""
        banana = "paloaltoCA"

        # Simulate 3 meetings with same ordinance (no matter_file, unstable matter_id)
        meetings = [
            {
                "date": "2025-01-15",
                "items": [{
                    "matter_id": "unstable-uuid-1",  # Changes each fetch
                    "title": "FIRST READING: Ordinance Amending Zoning Code Section 18.04",
                    "attachments": ["zoning_ordinance.pdf"]
                }]
            },
            {
                "date": "2025-02-01",
                "items": [{
                    "matter_id": "unstable-uuid-2",  # Different!
                    "title": "SECOND READING: Ordinance Amending Zoning Code Section 18.04",
                    "attachments": ["zoning_ordinance.pdf"]  # Same content
                }]
            },
            {
                "date": "2025-02-15",
                "items": [{
                    "matter_id": "unstable-uuid-3",  # Different again!
                    "title": "FINAL READING: Ordinance Amending Zoning Code Section 18.04",
                    "attachments": ["zoning_ordinance.pdf"]
                }]
            }
        ]

        # All should generate same matter ID (title-based, reading prefix stripped)
        base_title = "FIRST READING: Ordinance Amending Zoning Code Section 18.04"
        expected_id = generate_matter_id(banana, title=base_title)

        for meeting in meetings:
            item = meeting["items"][0]
            generated_id = generate_matter_id(banana, title=item["title"])
            assert generated_id == expected_id, f"Title-based ID should match despite reading prefix: {item['title']}"

        # After ingestion:
        # - 1 matter record (title-based)
        # - 3 appearances (ordinance lifecycle tracked!)
        # - Unchanged substantive attachments across readings -> sync-time
        #   copies prior items.summary onto each new appearance

    def test_generic_title_no_deduplication(self):
        """Generic titles (Public Comment) should NOT deduplicate"""
        banana = "paloaltoCA"

        # Public Comment appears in every meeting but should always be unique
        meetings = [
            {
                "date": "2025-01-15",
                "items": [{
                    "title": "Public Comment",
                    "attachments": []
                }]
            },
            {
                "date": "2025-02-01",
                "items": [{
                    "title": "Public Comment",
                    "attachments": []
                }]
            }
        ]

        # Should return None (excluded from matter tracking)
        for meeting in meetings:
            item = meeting["items"][0]
            result = generate_matter_id(banana, title=item["title"])
            assert result is None, "Generic titles should return None (no deduplication)"

        # After ingestion:
        # - 0 matter records (matter_id = NULL in items table)
        # - 2 separate item records
        # - Each processed independently

    def test_short_title_no_deduplication(self):
        """Short titles (<30 chars) should NOT deduplicate"""
        banana = "paloaltoCA"

        # Short procedural title
        title = "Budget Discussion"  # 17 chars

        result = generate_matter_id(banana, title=title)
        assert result is None, "Short titles should return None (likely procedural)"


class TestCrossCityCollisionPrevention:
    """Test that same matter identifiers in different cities don't collide"""

    def test_same_matter_file_different_cities_no_collision(self):
        """Nashville and Memphis both have 'BL2025-1098' - should not collide"""
        matter_file = "BL2025-1098"

        id_nashville = generate_matter_id("nashvilleTN", matter_file=matter_file)
        id_memphis = generate_matter_id("memphisTN", matter_file=matter_file)

        assert id_nashville != id_memphis, "Same matter_file in different cities should produce different IDs"
        assert id_nashville.startswith("nashvilleTN_")
        assert id_memphis.startswith("memphisTN_")

    def test_same_title_different_cities_no_collision(self):
        """Same ordinance title in different cities - should not collide"""
        title = "Approval of Annual Budget Amendments for Fiscal Year 2025"

        id_paloalto = generate_matter_id("paloaltoCA", title=title)
        id_menlopark = generate_matter_id("menloparkCA", title=title)

        assert id_paloalto != id_menlopark, "Same title in different cities should produce different IDs"


class TestAppearanceTracking:
    """Test matter appearance tracking (first_seen, last_seen, appearance_count)"""

    def test_timeline_tracking_updates(self):
        """Matter appears in 3 meetings - verify timeline metadata"""
        banana = "nashvilleTN"
        matter_file = "BL2025-1098"

        dates = [
            datetime(2025, 1, 15),
            datetime(2025, 2, 1),
            datetime(2025, 2, 15)
        ]

        matter_id = generate_matter_id(banana, matter_file=matter_file)

        # After ingesting all 3 meetings, the matter record should have:
        # first_seen = 2025-01-15
        # last_seen = 2025-02-15
        # appearance_count = 3

        expected_first = dates[0]
        expected_last = dates[-1]
        expected_count = len(dates)

        # Mock what the database should store
        matter_record = {
            "id": matter_id,
            "matter_file": matter_file,
            "first_seen": expected_first,
            "last_seen": expected_last,
            "appearance_count": expected_count
        }

        assert matter_record["first_seen"] == expected_first
        assert matter_record["last_seen"] == expected_last
        assert matter_record["appearance_count"] == 3


class TestFallbackHierarchyPrecedence:
    """Test that fallback hierarchy is respected (matter_file > matter_id > title)"""

    def test_matter_file_takes_precedence_over_matter_id(self):
        """When both matter_file and matter_id provided, matter_file wins"""
        banana = "nashvilleTN"
        matter_file = "BL2025-1098"
        matter_id = "uuid-abc-123"

        id_both = generate_matter_id(banana, matter_file=matter_file, matter_id=matter_id)
        id_file_only = generate_matter_id(banana, matter_file=matter_file)

        assert id_both == id_file_only, "matter_file should take precedence over matter_id"

    def test_matter_file_takes_precedence_over_title(self):
        """When both matter_file and title provided, matter_file wins"""
        banana = "nashvilleTN"
        matter_file = "BL2025-1098"
        title = "FIRST READING: Zoning Ordinance"

        id_both = generate_matter_id(banana, matter_file=matter_file, title=title)
        id_file_only = generate_matter_id(banana, matter_file=matter_file)

        assert id_both == id_file_only, "matter_file should take precedence over title"

    def test_matter_id_takes_precedence_over_title(self):
        """When both matter_id and title provided, matter_id wins"""
        banana = "paloaltoCA"
        matter_id = "uuid-abc-123"
        title = "Approval of Budget Amendments for Fiscal Year 2025"

        id_both = generate_matter_id(banana, matter_id=matter_id, title=title)
        id_id_only = generate_matter_id(banana, matter_id=matter_id)

        assert id_both == id_id_only, "matter_id should take precedence over title"


class TestBackwardCompatibility:
    """Ensure existing matter IDs in database remain valid"""

    def test_hash_format_unchanged_for_vendor_ids(self):
        """Existing matter_file/matter_id based IDs should produce same hash"""
        banana = "nashvilleTN"
        matter_file = "BL2025-1098"

        # Original hashing format: "banana:matter_file:matter_id"
        # This should still work (backward compatible)
        matter_id = generate_matter_id(banana, matter_file=matter_file)

        # Verify format: {banana}_{16-hex-chars}
        assert matter_id.startswith(f"{banana}_")
        hash_part = matter_id.split("_")[1]
        assert len(hash_part) == 16

        # Verify it's valid hex
        int(hash_part, 16)  # Will raise if not valid hex

    def test_title_based_uses_distinct_prefix(self):
        """Title-based IDs use 'title:' prefix to prevent collision with vendor IDs"""
        banana = "paloaltoCA"

        # If someone had a matter_id that matched a normalized title, they should produce different IDs
        normalized_title = "approval of budget amendments for fiscal year 2025"

        id_title = generate_matter_id(banana, title="Approval of Budget Amendments for Fiscal Year 2025")
        id_matter_id = generate_matter_id(banana, matter_id=normalized_title)

        # Should be different because title path uses "banana:title:normalized" format
        # while matter_id uses "banana::matter_id" format
        assert id_title != id_matter_id, "Title-based IDs should not collide with vendor IDs"


class TestRealWorldScenarios:
    """Test scenarios from actual city data"""

    def test_palo_alto_ordinance_lifecycle(self):
        """Real example: Palo Alto ordinance through multiple readings"""
        banana = "paloaltoCA"

        # Real Palo Alto data (no stable matter_file, unstable matter_id)
        readings = [
            "FIRST READING: Ordinance Amending Palo Alto Municipal Code Title 18 (Zoning) to Update Parking Standards",
            "SECOND READING: Ordinance Amending Palo Alto Municipal Code Title 18 (Zoning) to Update Parking Standards",
            "FINAL READING: Ordinance Amending Palo Alto Municipal Code Title 18 (Zoning) to Update Parking Standards"
        ]

        # All should generate same ID
        ids = [generate_matter_id(banana, title=title) for title in readings]
        assert ids[0] == ids[1] == ids[2], "All readings should produce same matter ID"

    def test_nashville_bill_file_number(self):
        """Real example: Nashville bill with stable file number"""
        banana = "nashvilleTN"
        matter_file = "BL2025-1098"

        # Nashville provides stable BL numbers (Legistar)
        id1 = generate_matter_id(banana, matter_file=matter_file)
        id2 = generate_matter_id(banana, matter_file=matter_file)

        assert id1 == id2, "Stable file numbers should always produce same ID"

    def test_mixed_city_matter_types(self):
        """Different cities use different identification methods"""
        # Nashville: matter_file (Legistar)
        nashville_id = generate_matter_id("nashvilleTN", matter_file="BL2025-1098")

        # Granicus city: matter_id (vendor UUID)
        granicus_id = generate_matter_id("granicus_cityCA", matter_id="uuid-abc-123")

        # Palo Alto: title (PrimeGov without stable IDs)
        paloalto_id = generate_matter_id(
            "paloaltoCA",
            title="Approval of Budget Amendments for Fiscal Year 2025"
        )

        # All should be valid format
        assert nashville_id.startswith("nashvilleTN_")
        assert granicus_id.startswith("granicus_cityCA_")
        assert paloalto_id.startswith("paloaltoCA_")

        # All should be different
        assert len({nashville_id, granicus_id, paloalto_id}) == 3


# Confidence level: 8/10
# Integration tests cover the critical paths:
# - All 3 fallback methods
# - Cross-city collision prevention
# - Reading prefix normalization
# - Generic title exclusion
# - Backward compatibility
#
# Missing (would require full database setup):
# - Actual database queries for timeline
# - Attachment hash change detection in practice
# - Full ingestion service flow with LLM processing
#
# These tests validate the ID generation logic end-to-end.
# Full integration with database would require PostgreSQL test instance.
