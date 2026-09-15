"""Discovery must reject plausible-looking cross-city and stale matches."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from scripts.discover_vendor_slugs import (
    address_matches,
    civicclerk_evidence,
    learn_patterns,
    load_targets,
    read_limited,
    templates,
    unique_leads,
)

NOW = datetime(2026, 9, 13, tzinfo=timezone.utc)
TARGET = {"name": "Newark", "state": "DE"}


def event(**changes):
    row = {
        "id": 1,
        "eventName": "City Council Meeting",
        "startDateTime": "2026-09-01T18:00:00Z",
        "eventLocation": {"city": "Newark", "state": "Delaware"},
        "agendaId": 10,
        "hasAgenda": True,
        "publishedFiles": [{"fileId": 42, "type": "Agenda Packet"}],
    }
    row.update(changes)
    return row


@pytest.mark.parametrize(
    "location",
    [
        {"city": "Newark", "state": "NJ"},
        {"city": "Newark", "state": "New Jersey"},
        {"city": "Newark", "state": "Delaware County"},
        {"city": "South Newark", "state": "DE"},
        {"city": "Newark", "state": ""},
        None,
    ],
)
def test_address_does_not_guess_state_or_neighbor(location):
    assert not address_matches(location, **{"name": "Newark", "state": "DE"})


def test_exact_structured_address_and_municipal_agenda_pass():
    assert civicclerk_evidence({"value": [event()]}, TARGET, NOW)


@pytest.mark.parametrize(
    "changes",
    [
        {"eventName": "Newark School Board"},
        {"eventName": "County Commissioners"},
        {"eventName": "Library Board of Trustees"},
        {"startDateTime": "2025-09-01T18:00:00Z"},
        {"startDateTime": "2027-09-01T18:00:00Z"},
        {"startDateTime": None},
        {"agendaId": 0},
        {"hasAgenda": False},
        {"publishedFiles": []},
        {"publishedFiles": [{"fileId": 42, "type": "Minutes"}]},
        {"eventLocation": {"city": "Newark", "state": "New Jersey"}},
    ],
)
def test_calendar_or_wrong_government_is_not_verified(changes):
    assert not civicclerk_evidence({"value": [event(**changes)]}, TARGET, NOW)


def test_name_folding_preserves_accents_and_apostrophes():
    assert templates("La Cañada Flintridge", "CA")["cityst"] == "lacanadaflintridgeca"
    assert templates("Coeur d'Alene", "ID")["cityst"] == "coeurdaleneid"


def test_patterns_ignore_unsynced_inactive_and_school_records():
    rows = [
        dict(
            name="Arvada",
            state="CO",
            slug="arvadaco",
            vendor="civicclerk",
            type="city",
            status="active",
            meetings=1,
        ),
        dict(
            name="Newark",
            state="DE",
            slug="newark",
            vendor="civicclerk",
            type="city",
            status="active",
            meetings=0,
        ),
        dict(
            name="Newark",
            state="NJ",
            slug="newark",
            vendor="civicclerk",
            type="city",
            status="inactive",
            meetings=1,
        ),
        dict(
            name="Example",
            state="NJ",
            slug="example",
            vendor="civicclerk",
            type="school_district",
            status="active",
            meetings=1,
        ),
    ]
    assert learn_patterns(rows)["civicclerk"][0]["template"] == "cityst"
    assert len(learn_patterns(rows)["civicclerk"]) == 1


def test_seed_preserves_alias_banana_and_skips_existing_data(tmp_path):
    seed = tmp_path / "seed.csv"
    seed.write_text("city,state,banana\nSan Buenaventura,CA,venturaCA\nArvada,CO,\n")
    registry = [
        dict(
            banana="venturaCA",
            name="Ventura",
            state="CA",
            type="city",
            status="active",
            meetings=7,
        )
    ]
    rows = load_targets(seed, registry)
    assert [(r["name"], r["banana"]) for r in rows] == [("Arvada", "arvadaCO")]


@pytest.mark.asyncio
async def test_response_reads_every_chunk_and_enforces_cap():
    class Content:
        async def iter_chunked(self, size):
            for chunk in [b'{"val', b'ue":', b"[]}"]:
                yield chunk

    response = SimpleNamespace(content=Content())
    assert await read_limited(response) == b'{"value":[]}'
    with pytest.raises(ValueError, match="response_too_large"):
        await read_limited(response, limit=5)


def test_resume_deduplicates_completed_leads(tmp_path):
    import json

    base = dict(name="Arvada", state="CO", vendor="civicclerk", slug="arvadaco")
    records = [
        base,
        dict(base, body_complete=True, identity_events=0),
        dict(base, body_complete=True, identity_events=2),
    ]
    (tmp_path / "leads.jsonl").write_text("\n".join(json.dumps(r) for r in records))
    assert len(unique_leads(tmp_path)) == 1
    assert unique_leads(tmp_path)[0]["identity_events"] == 2


@pytest.mark.parametrize(
    "title,address,expected",
    [
        ("Agenda Center • Newark, DE • CivicEngage", "Newark, DE 19711", True),
        ("Agenda Center • Newark, NJ • CivicEngage", "Newark, NJ 07102", False),
        (
            "Agenda Center • South Newark, DE • CivicEngage",
            "South Newark, DE 19711",
            False,
        ),
        (
            "Agenda Center • Newark, DE • CivicEngage",
            "Department of Parks and Recreation",
            False,
        ),
        ("Agenda Center • Newark, DE • CivicEngage", "Newark, NJ 07102", False),
        (
            "Agenda Center • Newark, DE • CivicEngage",
            "<script>Newark, DE 19711</script>",
            False,
        ),
        ("Agenda Center • Newark County, DE • CivicEngage", "Newark, DE 19711", False),
    ],
)
def test_html_identity_requires_matching_brand_and_postal_address(
    title, address, expected
):
    from scripts.discover_vendor_slugs import html_identity

    assert (
        bool(html_identity(f"<title>{title}</title><footer>{address}</footer>", TARGET))
        is expected
    )


def test_verification_requires_actual_adapter_evidence():
    from scripts.discover_vendor_slugs import valid_verification

    now = datetime.now(timezone.utc).isoformat()
    record = dict(
        TARGET,
        vendor="civicclerk",
        status="verified",
        http_status=200,
        body_complete=True,
        event_date=now,
        location={"city": "Newark", "state": "DE"},
        verification="structured_city_state_address_and_recent_municipal_agenda_items",
        items_with_attachments=2,
    )
    assert valid_verification(record)
    assert not valid_verification(dict(record, http_status=404))
    assert not valid_verification(dict(record, body_complete=False))
    assert not valid_verification(dict(record, status="lead"))
    assert not valid_verification(dict(record, items_with_attachments=0))
    assert not valid_verification(dict(record, vendor="unknown"))
    assert not valid_verification(dict(record, event_date="2020-01-01"))


def test_existing_records_are_protected_from_discovery_overwrites():
    from scripts.discover_vendor_slugs import existing_action

    record = dict(
        banana="newarkDE",
        name="Newark",
        state="DE",
        vendor="civicclerk",
        slug="newarkde",
    )
    existing = dict(
        record, vendor="granicus", slug="newark", type="city", status="active"
    )
    assert existing_action(record, existing, existing, False) == "repair"
    assert existing_action(record, existing, existing, True) == "protected_has_meetings"
    assert existing_action(record, existing, None, False) == "changed_since_snapshot"
    assert (
        existing_action(record, dict(existing, slug="corrected"), existing, False)
        == "changed_since_snapshot"
    )
    for changes in [
        dict(state="NJ"),
        dict(name="South Newark"),
        dict(type="county"),
        dict(status="inactive"),
        dict(banana="newarkNJ"),
    ]:
        assert (
            existing_action(record, dict(existing, **changes), existing, False)
            == "identity_or_status_conflict"
        )
    assert (
        existing_action(
            record,
            dict(existing, vendor="civicclerk", slug="newarkde"),
            existing,
            False,
        )
        == "already_configured"
    )


def source(vendor, status="verified"):
    return dict(
        banana="exampleCA",
        name="Example",
        state="CA",
        vendor=vendor,
        slug=vendor + "example",
        status=status,
    )


def test_dual_platform_discovery_requires_coverage_decision():
    from scripts.discover_vendor_slugs import select_sources

    chosen, held = select_sources([source("civicclerk"), source("civicplus")], {})
    assert not chosen and held[0]["reason"] == "source_coverage_decision_required"
    # Even an unverified Plus lead must not be silently discarded by a Clerk-first rank.
    chosen, held = select_sources(
        [source("civicclerk"), source("civicplus", "review")], {}
    )
    assert not chosen and held


def test_explicit_plus_superset_choice_overrides_clerk_format_preference():
    from scripts.discover_vendor_slugs import select_sources

    decision = {
        "action": "select",
        "primary": {"vendor": "civicplus", "slug": "civicplusexample"},
        "reason": "Clerk is a strict observed subset",
    }
    chosen, held = select_sources(
        [source("civicclerk"), source("civicplus")], {"exampleCA": decision}
    )
    assert not held and chosen[0]["vendor"] == "civicplus"


def test_split_sources_require_independent_verification_and_disjoint_bodies():
    from scripts.discover_vendor_slugs import select_sources

    decision = {
        "action": "select",
        "primary": {"vendor": "civicclerk", "slug": "civicclerkexample"},
        "extras": [{"vendor": "civicplus", "slug": "civicplusexample"}],
        "relationship": "disjoint_verified_bodies",
        "reason": "Council versus parks board",
    }
    chosen, held = select_sources(
        [source("civicclerk"), source("civicplus")], {"exampleCA": decision}
    )
    assert not held and chosen[0]["extra_vendors"] == decision["extras"]
    chosen, held = select_sources(
        [source("civicclerk"), source("civicplus", "review")], {"exampleCA": decision}
    )
    assert (
        not chosen and held[0]["reason"] == "chosen_source_not_independently_verified"
    )
    chosen, held = select_sources(
        [source("civicclerk"), source("civicplus")],
        {"exampleCA": dict(decision, relationship="partial_overlap")},
    )
    assert (
        not chosen and held[0]["reason"] == "overlapping_sources_need_scoped_ingestion"
    )


def test_new_review_invalidates_older_verified_receipt():
    from scripts.discover_vendor_slugs import select_sources

    chosen, held = select_sources(
        [source("civicclerk"), source("civicclerk", "review")], {}
    )
    assert not chosen
