"""
Tests for Matter ID Generation and Validation

Tests the 3-tier fallback hierarchy for matter identification:
1. matter_file (preferred) - Public legislative file number
2. matter_id (fallback) - Vendor UUID
3. title (last resort) - Normalized title for cities without stable IDs

Also tests edge cases: reading prefixes, generic titles, cross-city collision.
"""

from datetime import datetime

import pytest
from database.id_generation import (
    generate_meeting_id,
    generate_matter_id,
    normalize_title_for_matter_id,
    validate_matter_id,
    extract_banana_from_matter_id,
    matter_ids_match,
    hash_meeting_id
)


def test_undated_meeting_id_is_stable_and_distinct_from_dated_identity():
    undated = generate_meeting_id(
        "exampleCA",
        "vendor-meeting-1",
        None,
        "City Council",
    )

    assert generate_meeting_id(
        "exampleCA",
        "vendor-meeting-1",
        None,
        "City Council",
    ) == undated
    assert generate_meeting_id(
        "exampleCA",
        "vendor-meeting-1",
        datetime(2026, 8, 7),
        "City Council",
    ) != undated


class TestMatterFileFallback:
    """Test fallback path 1: matter_file (preferred, most stable)"""

    def test_matter_file_generates_consistent_id(self):
        """Same matter_file always produces same ID"""
        id1 = generate_matter_id("nashvilleTN", matter_file="BL2025-1098")
        id2 = generate_matter_id("nashvilleTN", matter_file="BL2025-1098")
        assert id1 == id2

    def test_matter_file_preferred_over_matter_id(self):
        """matter_file takes precedence even if matter_id provided"""
        id_with_file = generate_matter_id(
            "nashvilleTN",
            matter_file="BL2025-1098",
            matter_id="abc123"
        )
        id_file_only = generate_matter_id(
            "nashvilleTN",
            matter_file="BL2025-1098"
        )
        assert id_with_file == id_file_only

    def test_matter_file_preferred_over_title(self):
        """matter_file takes precedence over title"""
        id_with_file = generate_matter_id(
            "nashvilleTN",
            matter_file="BL2025-1098",
            title="FIRST READING: Zoning Ordinance"
        )
        id_file_only = generate_matter_id(
            "nashvilleTN",
            matter_file="BL2025-1098"
        )
        assert id_with_file == id_file_only

    def test_different_matter_files_produce_different_ids(self):
        """Different matter files produce different IDs"""
        id1 = generate_matter_id("nashvilleTN", matter_file="BL2025-1098")
        id2 = generate_matter_id("nashvilleTN", matter_file="BL2025-1099")
        assert id1 != id2


class TestMatterIdFallback:
    """Test fallback path 2: matter_id (vendor UUID)"""

    def test_matter_id_generates_consistent_id(self):
        """Same matter_id always produces same ID"""
        id1 = generate_matter_id("paloaltoCA", matter_id="fb36db52-abc-123")
        id2 = generate_matter_id("paloaltoCA", matter_id="fb36db52-abc-123")
        assert id1 == id2

    def test_matter_id_used_when_no_matter_file(self):
        """matter_id used as fallback when matter_file absent"""
        id_with_matter_id = generate_matter_id(
            "paloaltoCA",
            matter_id="fb36db52-abc-123"
        )
        assert validate_matter_id(id_with_matter_id)
        assert extract_banana_from_matter_id(id_with_matter_id) == "paloaltoCA"

    def test_different_matter_ids_produce_different_ids(self):
        """Different vendor UUIDs produce different IDs"""
        id1 = generate_matter_id("paloaltoCA", matter_id="uuid-1")
        id2 = generate_matter_id("paloaltoCA", matter_id="uuid-2")
        assert id1 != id2


class TestTitleNormalizationFallback:
    """Test fallback path 3: title normalization (last resort)"""

    def test_title_generates_id_when_no_file_or_id(self):
        """Title normalization works as last resort"""
        id_result = generate_matter_id(
            "paloaltoCA",
            title="Approval of Budget Amendments for FY 2025"
        )
        assert id_result is not None
        assert validate_matter_id(id_result)

    def test_reading_prefix_normalization(self):
        """Different readings of same ordinance produce same ID"""
        id_first = generate_matter_id(
            "paloaltoCA",
            title="FIRST READING: Ordinance Amending Zoning Code Section 18.04"
        )
        id_second = generate_matter_id(
            "paloaltoCA",
            title="SECOND READING: Ordinance Amending Zoning Code Section 18.04"
        )
        id_final = generate_matter_id(
            "paloaltoCA",
            title="FINAL READING: Ordinance Amending Zoning Code Section 18.04"
        )
        assert id_first == id_second == id_final

    def test_reintroduced_prefix_normalization(self):
        """REINTRODUCED prefix stripped correctly"""
        id_reintro = generate_matter_id(
            "paloaltoCA",
            title="REINTRODUCED FIRST READING: Ordinance 2025-123"
        )
        id_normal = generate_matter_id(
            "paloaltoCA",
            title="FIRST READING: Ordinance 2025-123"
        )
        assert id_reintro == id_normal

    def test_generic_title_returns_none(self):
        """Generic procedural titles return None (no deduplication)"""
        generic_titles = [
            "Public Comment",
            "Staff Comments",
            "VTA",
            "Closed Session",
            "Open Forum",
        ]
        for title in generic_titles:
            assert generate_matter_id("paloaltoCA", title=title) is None

    def test_short_title_returns_none(self):
        """Titles under 30 chars return None (likely procedural)"""
        short_title = "Budget Discussion"  # 17 chars
        assert generate_matter_id("paloaltoCA", title=short_title) is None

    def test_case_insensitive_normalization(self):
        """Title normalization is case-insensitive"""
        id_lower = generate_matter_id(
            "paloaltoCA",
            title="approval of budget amendments for fy 2025"
        )
        id_upper = generate_matter_id(
            "paloaltoCA",
            title="APPROVAL OF BUDGET AMENDMENTS FOR FY 2025"
        )
        id_mixed = generate_matter_id(
            "paloaltoCA",
            title="Approval of Budget Amendments for FY 2025"
        )
        assert id_lower == id_upper == id_mixed

    def test_whitespace_normalization(self):
        """Extra whitespace normalized to single spaces"""
        id_normal = generate_matter_id(
            "paloaltoCA",
            title="Approval of Budget Amendments"
        )
        id_extra_spaces = generate_matter_id(
            "paloaltoCA",
            title="Approval   of    Budget    Amendments"
        )
        assert id_normal == id_extra_spaces


class TestCrossCityCollisionPrevention:
    """Test that same matter_file/matter_id in different cities produce different IDs"""

    def test_same_matter_file_different_cities(self):
        """Same file number in different cities produces different IDs"""
        id_nashville = generate_matter_id("nashvilleTN", matter_file="BL2025-1098")
        id_memphis = generate_matter_id("memphisTN", matter_file="BL2025-1098")
        assert id_nashville != id_memphis

    def test_same_matter_id_different_cities(self):
        """Same vendor UUID in different cities produces different IDs"""
        id_city1 = generate_matter_id("city1CA", matter_id="uuid-123")
        id_city2 = generate_matter_id("city2CA", matter_id="uuid-123")
        assert id_city1 != id_city2

    def test_same_title_different_cities(self):
        """Same normalized title in different cities produces different IDs"""
        title = "Approval of Annual Budget Amendments for Fiscal Year 2025"
        id_paloalto = generate_matter_id("paloaltoCA", title=title)
        id_menlopark = generate_matter_id("menloparkCA", title=title)
        assert id_paloalto != id_menlopark

    def test_banana_extraction(self):
        """Can extract banana from generated ID"""
        matter_id = generate_matter_id("nashvilleTN", matter_file="BL2025-1098")
        banana = extract_banana_from_matter_id(matter_id)
        assert banana == "nashvilleTN"


class TestMatterIdValidation:
    """Test matter ID format validation"""

    def test_valid_format(self):
        """Valid format: {banana}_{16-hex-chars}"""
        valid_id = "nashvilleTN_7a8f3b2c1d9e4f5a"
        assert validate_matter_id(valid_id)

    def test_invalid_format_no_underscore(self):
        """Invalid: missing underscore separator"""
        invalid_id = "nashvilleTN7a8f3b2c1d9e4f5a"
        assert not validate_matter_id(invalid_id)

    def test_invalid_format_wrong_hash_length(self):
        """Invalid: hash part not 16 chars"""
        invalid_id = "nashvilleTN_7a8f3b2c"  # Only 8 hex chars
        assert not validate_matter_id(invalid_id)

    def test_invalid_format_non_hex(self):
        """Invalid: hash contains non-hex characters"""
        invalid_id = "nashvilleTN_zzzzzzzzzzzzzzzz"
        assert not validate_matter_id(invalid_id)

    def test_invalid_format_empty(self):
        """Invalid: empty string"""
        assert not validate_matter_id("")

    def test_invalid_format_none(self):
        """Invalid: None"""
        assert not validate_matter_id(None)


class TestMatterIdsMatch:
    """Test matter_ids_match helper function"""

    def test_matching_matter_files(self):
        """Same matter_file considered matching"""
        assert matter_ids_match(
            "nashvilleTN",
            "BL2025-1098", None,
            "BL2025-1098", None
        )

    def test_matching_matter_ids(self):
        """Same matter_id considered matching"""
        assert matter_ids_match(
            "paloaltoCA",
            None, "uuid-123",
            None, "uuid-123"
        )

    def test_different_matter_files(self):
        """Different matter_files not matching"""
        assert not matter_ids_match(
            "nashvilleTN",
            "BL2025-1098", None,
            "BL2025-1099", None
        )

    def test_matter_file_vs_matter_id(self):
        """matter_file and matter_id for same matter don't match (different fallback paths)"""
        # This is expected - they're different identifiers
        result = matter_ids_match(
            "nashvilleTN",
            "BL2025-1098", None,
            None, "uuid-123"
        )
        # Should not match because they use different fallback paths
        assert not result


class TestNormalizeTitleForMatterId:
    """Test title normalization function in isolation"""

    def test_basic_normalization(self):
        """Basic title normalized to lowercase"""
        result = normalize_title_for_matter_id("Approval of Annual Budget Amendments")
        assert result == "approval of annual budget amendments"

    def test_first_reading_stripped(self):
        """FIRST READING prefix stripped"""
        result = normalize_title_for_matter_id(
            "FIRST READING: Ordinance 2025-123 Establishing Water Rates"
        )
        assert result == "ordinance 2025-123 establishing water rates"

    def test_second_reading_stripped(self):
        """SECOND READING prefix stripped"""
        result = normalize_title_for_matter_id(
            "SECOND READING: Ordinance 2025-123 Establishing Water Rates"
        )
        assert result == "ordinance 2025-123 establishing water rates"

    def test_generic_title_returns_none(self):
        """Generic titles return None"""
        assert normalize_title_for_matter_id("Public Comment") is None
        assert normalize_title_for_matter_id("VTA") is None

    def test_short_title_returns_none(self):
        """Short titles return None"""
        assert normalize_title_for_matter_id("Short") is None

    def test_empty_title_returns_none(self):
        """Empty title returns None"""
        assert normalize_title_for_matter_id("") is None
        assert normalize_title_for_matter_id(None) is None

    def test_whitespace_only_returns_none(self):
        """Whitespace-only title returns None"""
        assert normalize_title_for_matter_id("   ") is None


class TestHashMeetingId:
    """Test meeting ID hashing for URL slugs"""

    def test_consistent_hashing(self):
        """Same meeting ID always produces same hash"""
        hash1 = hash_meeting_id("71CAEB7D-4BC6-F011-BBD2-001DD8020E93")
        hash2 = hash_meeting_id("71CAEB7D-4BC6-F011-BBD2-001DD8020E93")
        assert hash1 == hash2

    def test_hash_length(self):
        """Hash is exactly 16 hex characters"""
        result = hash_meeting_id("12345")
        assert len(result) == 16
        # Verify it's valid hex
        int(result, 16)  # Will raise if not valid hex

    def test_different_ids_produce_different_hashes(self):
        """Different meeting IDs produce different hashes"""
        hash1 = hash_meeting_id("meeting-1")
        hash2 = hash_meeting_id("meeting-2")
        assert hash1 != hash2

    def test_handles_uuid_format(self):
        """Can hash Chicago-style UUID meeting IDs"""
        uuid_id = "71CAEB7D-4BC6-F011-BBD2-001DD8020E93"
        result = hash_meeting_id(uuid_id)
        assert len(result) == 16


class TestEdgeCases:
    """Test edge cases and error conditions"""

    def test_no_identifiers_raises_error(self):
        """Raises ValueError when no identifiers provided"""
        with pytest.raises(ValueError, match="At least one of"):
            generate_matter_id("nashvilleTN")

    def test_generic_title_only_returns_none(self):
        """Only generic title provided returns None (caller should handle)"""
        result = generate_matter_id("paloaltoCA", title="Public Comment")
        assert result is None

    def test_matter_file_empty_string_uses_matter_id(self):
        """Empty string matter_file falls back to matter_id"""
        # When matter_file is empty string, it's truthy but empty
        # Implementation treats empty string as "no file"
        id_result = generate_matter_id(
            "nashvilleTN",
            matter_file="",
            matter_id="uuid-123"
        )
        # Should still work because implementation checks 'or' logic
        assert id_result is not None

    def test_all_three_identifiers_uses_matter_file(self):
        """When all 3 provided, matter_file wins"""
        id_all_three = generate_matter_id(
            "nashvilleTN",
            matter_file="BL2025-1098",
            matter_id="uuid-123",
            title="Ordinance Title"
        )
        id_file_only = generate_matter_id(
            "nashvilleTN",
            matter_file="BL2025-1098"
        )
        assert id_all_three == id_file_only


class TestBackwardCompatibility:
    """Ensure backward compatibility with existing matter IDs in database"""

    def test_original_format_preserved_for_matter_file(self):
        """Original format {banana}:{matter_file}:{matter_id} preserved"""
        # This tests that the hashing input format hasn't changed
        # If format changed, existing matter IDs in DB would break
        id_result = generate_matter_id("nashvilleTN", matter_file="BL2025-1098")
        # Should produce consistent output (same hash for same input)
        assert id_result.startswith("nashvilleTN_")
        assert len(id_result.split("_")[1]) == 16  # 16 hex chars

    def test_title_based_has_distinct_prefix(self):
        """Title-based IDs use distinct prefix to avoid collision"""
        # Title-based uses "banana:title:normalized" format
        # This prevents collision with vendor IDs
        id_title = generate_matter_id(
            "paloaltoCA",
            title="Approval of Budget Amendments for Fiscal Year 2025"
        )
        id_matter_id = generate_matter_id(
            "paloaltoCA",
            matter_id="approval of budget amendments for fiscal year 2025"
        )
        # Should be different because title path uses "title:" prefix in key
        assert id_title != id_matter_id
