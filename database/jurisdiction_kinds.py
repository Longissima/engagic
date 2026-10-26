"""Jurisdiction kind: what a body does, beneath its five-way class.

Two levels, decided 2026-10-24 after the free-text type prompt grew 60 values:

- ``type`` is the class the pipeline and coverage reports branch on: city,
  county, school_district, special_district, state. It is never written
  directly; a trigger copies it from the kind's registry row
  (migration 064_jurisdiction_kinds).
- ``kind`` is the function a reader cares about (port, fire_district, transit),
  a foreign key into ``jurisdiction_kinds``, which owns the vocabulary, the
  class each kind belongs to, and its display label.

The kind comes from the body's own name first. The Census function code is a
fallback only: it filed mosquito abatement under health, watershed commissions
under natural resources, and ports under industrial development. Rules here
may only name kinds seeded by the migration; tests/test_jurisdiction_kinds.py
fails on drift.
"""

import re
from typing import Optional

GENERAL_KINDS = {"city": "city", "township": "township", "county": "county", "school_district": "school_district"}

# Pre-2026-10-24 type values that were state-level bodies. Name rules for
# special districts never run on these: "Commonwealth Transportation Board" is
# a state agency, not a transit system.
STATE_LEGACY = frozenset({
    "state", "state_agency", "water_quality_board", "natural_resources_commission", "environmental_agency",
    "energy_commission", "utility_commission", "water_commission", "corrections_oversight", "court",
    "grand_jury", "university", "university_system",
})

STATE_RULES = (
    (r"public service commission|public utilit(y|ies) commission|utilities and transportation commission", "public_utility_commission"),
    (r"regional water quality control board", "water_quality_board"),
    (r"\bcourt\b|grand jury", "court"),
    (r"universit", "university"),
)

# Ordered: the first match wins, so combined names resolve to their main
# function ("Water and Sewer Authority" is water before it is sewer, "Arrowbear
# Park County Water District" is water, not parks). A rule marked defer names
# structure, not function ("Improvement District", "Tax District", "Municipal
# Utility District"): a specific old label beats it, because Norwalk's 3rd Tax
# District runs an electric utility and SMUD is power, not water.
SPECIAL_RULES = (
    (r"^(virginia|commonwealth of|state of)\b.*\b(board|department|commission)\b|\bstate (air|water) .*board\b", "state_agency", False),
    (r"workers'? comp|insurance|risk management|risk pool|excess liability|liability pool|pooling authority|\bjpia\b|self[- ]?insur", "risk_pool", False),
    (r"retirement|pension", "retirement_system", False),
    (r"\bport of\b|\bport (authority|district|commission)\b|harbou?r (district|commission|recreation)", "port", False),
    (r"airport", "airport_authority", False),
    (r"metropolitan planning|planning organization|planning council|council of governments|regional planning|planning commission|transportation commission|agency for planning", "regional_planning_agency", False),
    (r"interstate|river basin commission|great lakes commission", "interstate_commission", False),
    (r"\btransit\b|rapid transit|public transportation|transportation authority|\btrax\b", "transit", False),
    (r"\btoll\b|bridge and tunnel|bridge (commission|authority)|river and bay authority", "toll_authority", False),
    (r"\bgrid\b|interconnection|reliability council", "grid_operator", False),
    (r"9-?1-?1|emergency communications|emergency network", "emergency_communications_district", False),
    (r"mosquito|vector|abatement|tristeza|\bpest\b", "vector_control_district", False),
    (r"\bfire\b|emergency services? district|\besd\b|ambulance|emergency services authority", "fire_district", False),
    (r"hospital|health ?care|medical district", "hospital_district", False),
    (r"children'?s services|social services", "social_services_district", False),
    (r"workforce", "workforce_board", False),
    (r"air quality|air pollution|clean air", "air_quality_district", False),
    (r"public health|health (service )?district|mental health|\bmhmr\b|ctr for mental", "health_district", False),
    (r"cemetery", "cemetery_district", False),
    (r"library", "library_district", False),
    (r"community college|college(s)? district|\bcollege\b", "community_college_district", False),
    (r"soil (and|&) water|resource conservation|natural resources district", "conservation_district", False),
    (r"groundwater|aquif|subsidence|sustainability agency|water management (district|agency)|water conservation district", "water_management_district", False),
    (r"water control district", "flood_control_district", False),
    (r"water and (sewer|sanitation|wastewater)|water & sewer|sewer and water|water and stormwater|rural water", "water_district", False),
    (r"sanitary|sanitation|\bsewer|wastewater|water reclamation|water pollution control|sewerage", "sanitation_district", False),
    (r"irrigation|water storage district", "irrigation_district", False),
    (r"public utility district|utility (district|authority)|\bmud\b|river authority|valley authority", "water_district", True),
    (r"electric|\bpower\b|energy|light and power|\bgas\b", "utility", False),
    (r"\bwater\b|aqueduct", "water_district", False),
    (r"flood|drainage|levee|reclamation district|stormwater", "flood_control_district", False),
    (r"solid waste|waste management|recycling|refuse|resource recovery|composting|waste disposal", "solid_waste_authority", False),
    (r"housing", "housing_authority", False),
    (r"watershed|waterway|lake district|lakes? (protection|management|improvement)|protection and rehabilitation", "watershed_district", False),
    (r"metropolitan district|community development district|community services district|improvement district|municipal service district|general improvement|public service district", "improvement_district", True),
    (r"redevelopment|urban renewal|industrial development|economic development|development authority", "economic_development_authority", False),
    (r"tax district", "tax_district", True),
    (r"conservation district|conservancy", "conservation_district", False),
    (r"\bparks?\b.*district|recreation|forest preserve|park authority", "park_district", False),
    (r"highway|road district", "highway_district", False),
)

# Bodies whose names mislead every rule. Last resort, one line of why each.
KIND_OVERRIDES = {
    "capAZ": "water_district",  # runs the Central Arizona Project canal; "conservation" is historical
    "goldengatebridgedistrictCA": "transit",  # operates Golden Gate Transit and ferries alongside the bridge
    "mchenrycountyconservationdistrictIL": "park_district",  # Illinois county open-space and park agency
    "steubenlakesregionalwastedistrictIN": "sanitation_district",  # regional sewer district
    "devilslakewaterimprovementdistrictOR": "watershed_district",  # manages the lake, supplies no water
    "sanfrancisquitocreekjointpowersauthorityCA": "flood_control_district",  # creek flood protection JPA
    "waterconservationgardenauthorityCA": "special_district",  # demonstration garden JPA
    "livermoreamadorvalleywatermanagementagencyCA": "sanitation_district",  # wastewater export JPA
    "lcraTX": "utility",  # wholesale power first; water supply second
    "clallamcountypublicutilitydistrictWA": "utility",  # electric first; WA PUDs split between power and water
    "lcmuaTX": "water_district",  # Lake Cities MUA: Texas water and sewer, labelled utility by Census
    "duncannonboroughmunicipalauthorityPA": "water_district",  # PA municipal authority running water and sewer
    "jamesislandpublicservicedistrictSC": "sanitation_district",  # sewer only
    "kittcomWA": "emergency_communications_district",  # Kittitas County 9-1-1 dispatch
    "richardsonbayregionalagencyCA": "port",  # manages the bay's anchorage and harbor
    "cfpuaNC": "water_district",  # Cape Fear PUA: water and sewer despite "utility"
}

# Old type value -> kind, for names that carry no function word.
LEGACY_KIND = {
    "water_reclamation_district": "sanitation_district",
    "regional_planning": "regional_planning_agency",
    "natural_resources_district": "conservation_district",
    "public_health": "health_district",
    "public_welfare_district": "social_services_district",
    "industrial_development_authority": "economic_development_authority",
    "redevelopment_authority": "economic_development_authority",
    "community_services_district": "improvement_district",
    "community_development_district": "improvement_district",
    "drainage_district": "flood_control_district",
    "reclamation_district": "flood_control_district",
    "forest_preserve_district": "park_district",
    "river_authority": "water_district",
    "air_quality_board": "air_quality_district",
    "river_basin_commission": "interstate_commission",
    "education_district": "special_district",
    "police_district": "special_district",
    "sports_authority": "special_district",
    "corrections_oversight": "state_agency",
    "natural_resources_commission": "state_agency",
    "environmental_agency": "state_agency",
    "energy_commission": "state_agency",
    "water_commission": "state_agency",
    "utility_commission": "public_utility_commission",
    "university_system": "university",
    "grand_jury": "court",
}

# Census FUNCTION code (first two digits of unit_type) -> kind, consulted only
# when the name says nothing. Codes from scripts/census_coverage.py.
CENSUS_FUNCTION_KIND = {
    "01": "airport_authority", "02": "cemetery_district", "24": "fire_district", "32": "health_district",
    "40": "hospital_district", "41": "economic_development_authority", "42": "housing_authority",
    "44": "highway_district", "45": "toll_authority", "50": "housing_authority", "51": "flood_control_district",
    "52": "library_district", "59": "conservation_district", "61": "park_district", "63": "flood_control_district",
    "64": "irrigation_district", "77": "social_services_district", "79": "social_services_district",
    "80": "sanitation_district", "81": "solid_waste_authority", "86": "flood_control_district", "87": "port",
    "88": "conservation_district", "91": "water_district", "92": "utility", "93": "utility", "94": "transit",
    "96": "fire_district", "97": "water_district", "98": "water_district",
}

_SPECIAL = tuple((re.compile(p, re.I), k, defer) for p, k, defer in SPECIAL_RULES)
_STATE = tuple((re.compile(p, re.I), k) for p, k in STATE_RULES)


def _legacy_kind(legacy: str) -> Optional[str]:
    """A specific function the old label already named, or None for generic/empty labels."""
    kind = LEGACY_KIND.get(legacy, legacy)
    return kind if kind and kind != "special_district" else None


def classify(name: str, legacy_type: Optional[str], census_unit_type: Optional[str] = None,
             banana: Optional[str] = None) -> str:
    """The kind for a body, from its name, then its Census function, then its old label."""
    if banana in KIND_OVERRIDES:
        return KIND_OVERRIDES[banana]
    legacy = (legacy_type or "").strip().lower()
    if legacy in GENERAL_KINDS:
        return GENERAL_KINDS[legacy]
    if legacy in STATE_LEGACY:
        if legacy == "state":
            return "state"
        for pattern, kind in _STATE:
            if pattern.search(name):
                return kind
        return LEGACY_KIND.get(legacy, "state_agency")
    if legacy == "public_health" and re.search(r"\bstate\b", name, re.I):
        return "state_agency"
    for pattern, kind, defer in _SPECIAL:
        if pattern.search(name):
            return (_legacy_kind(legacy) or kind) if defer else kind
    census_kind = CENSUS_FUNCTION_KIND.get((census_unit_type or "")[:2])
    return census_kind or _legacy_kind(legacy) or "special_district"
