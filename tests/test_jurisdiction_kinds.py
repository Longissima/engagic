"""The classifier only names registry kinds, and names beat structure words."""

import re
from pathlib import Path

import pytest

from database import jurisdiction_kinds as jk

MIGRATION = Path(__file__).resolve().parents[1] / "database" / "migrations" / "064_jurisdiction_kinds.sql"


def seeded_kinds() -> set[str]:
    seed = MIGRATION.read_text().split("INSERT INTO jurisdiction_kinds", 1)[1].split(";", 1)[0]
    return set(re.findall(r"\(\s*'([a-z0-9_]+)',\s*'[a-z_]+',", seed))


def test_every_kind_the_classifier_can_return_is_seeded():
    reachable = (
        set(jk.GENERAL_KINDS.values())
        | {k for _, k in jk.STATE_RULES}
        | {k for _, k, _ in jk.SPECIAL_RULES}
        | set(jk.LEGACY_KIND.values())
        | set(jk.CENSUS_FUNCTION_KIND.values())
        | set(jk.KIND_OVERRIDES.values())
        | {"state", "state_agency", "special_district"}
    )
    assert reachable - seeded_kinds() == set()


@pytest.mark.parametrize(
    "name, legacy, expected",
    [
        ("Port of Tillamook Bay", "special_district", "port"),
        ("Mackaye Harbor Water District", "water_district", "water_district"),
        ("Coos County Soil and Water Conservation District", "conservation_district", "conservation_district"),
        ("Sacramento Municipal Utility District", "utility", "utility"),
        ("Mason Creek Utility District", "special_district", "water_district"),
        ("Norwalk 3rd Tax District", "utility", "utility"),
        ("Amston Lake Tax District", "special_district", "tax_district"),
        ("Superstition Fire & Medical District", "fire_district", "fire_district"),
        ("Durham Mosquito Abatement District", "health_district", "vector_control_district"),
        ("Puget Sound Clean Air Agency", "air_quality_board", "air_quality_district"),
        ("Virginia State Air Pollution Control Board", "air_quality_board", "state_agency"),
        ("Florida Public Service Commission", "state_agency", "public_utility_commission"),
        ("Commonwealth Transportation Board", "state_agency", "state_agency"),
        ("Tarrant County 9-1-1 District", "special_district", "emergency_communications_district"),
        ("Marple Township", "township", "township"),
    ],
)
def test_names_resolve_to_their_function(name, legacy, expected):
    assert jk.classify(name, legacy) == expected


def test_census_code_only_fills_in_when_the_name_is_silent():
    assert jk.classify("Kittitas Example Board", "special_district", "87000") == "port"
    assert jk.classify("Port of Example", "special_district", "91000") == "port"
