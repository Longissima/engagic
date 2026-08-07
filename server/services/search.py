"""
Search service layer

Business logic for handling different search types
"""

from typing import cast, Dict, Any, List, Optional, TypedDict, Literal, Union
from typing_extensions import NotRequired
from difflib import get_close_matches
from database.db_postgres import Database
from server.services.meeting import get_meetings_for_listing
from server.utils.geo import parse_city_state_input, get_state_abbreviation, get_state_full_name
from server.utils.vendor_urls import get_vendor_source_url, get_vendor_display_name, get_vendor_source_urls

from config import get_logger

logger = get_logger(__name__)


async def _record_city_request(db: Database, banana: str) -> None:
    """Record city demand, logging failures without propagating."""
    try:
        await db.userland.record_city_request(banana)
        logger.info("city request recorded", banana=banana)
    except Exception as e:
        logger.warning("failed to record city request", banana=banana, error=str(e))


async def _log_city_search(db: Database, banana: str, query: str) -> None:
    """Log successful city search to activity_log for analytics."""
    try:
        await db.engagement.log_activity(
            user_id=None,
            session_id=None,
            action="search",
            entity_type="city",
            entity_id=banana,
            metadata={"query": query},
        )
    except Exception as e:
        logger.warning("failed to log city search", banana=banana, error=str(e))


def _apply_stats_to_options(
    options: List["CityOption"], stats: Dict[str, Dict[str, int]]
) -> None:
    """Apply meeting stats to city options in-place."""
    default_stats = {"total_meetings": 0, "meetings_with_packet": 0, "summarized_meetings": 0}
    for option in options:
        city_stats = stats.get(option["banana"], default_stats)
        option["total_meetings"] = city_stats["total_meetings"]
        option["meetings_with_packet"] = city_stats["meetings_with_packet"]
        option["summarized_meetings"] = city_stats["summarized_meetings"]


def _make_uncovered_option(city_name: str, state: str) -> "CityOption":
    """Create a city option for an uncovered city."""
    city_name_clean = city_name.lower().replace(" ", "")
    return {
        "city_name": city_name.title(),
        "state": state,
        "banana": f"{city_name_clean}{state}",
        "vendor": "unknown",
        "display_name": f"{city_name.title()}, {state}",
        "total_meetings": 0,
        "meetings_with_packet": 0,
        "summarized_meetings": 0,
        "covered": False,
    }


class CityOption(TypedDict):
    """City option for ambiguous search results (multiple cities match)."""
    city_name: str
    state: str
    banana: str
    vendor: str
    display_name: str
    total_meetings: int
    meetings_with_packet: int
    summarized_meetings: int
    covered: NotRequired[bool]  # True if we have this city in DB, False/missing if not


class SearchSuccessResponse(TypedDict):
    """Response when search finds a city with meetings."""
    success: Literal[True]
    city_name: str
    state: str
    banana: str
    vendor: str
    vendor_display_name: str
    source_url: Optional[str]
    source_urls: List[Dict[str, str]]  # Multi-body sources: [{"url": ..., "body": ...}]
    participation: Optional[Dict[str, Any]]
    meetings: List[Dict[str, Any]]
    cached: bool
    query: str
    type: str
    message: NotRequired[str]
    ambiguous: NotRequired[Literal[False]]


class SearchNotFoundResponse(TypedDict):
    """Response when search doesn't find the city or finds city without meetings."""
    success: Literal[False]
    message: str
    query: str
    type: str
    meetings: List[Any]
    ambiguous: NotRequired[Literal[False]]
    city_name: NotRequired[str]
    state: NotRequired[str]
    banana: NotRequired[str]
    vendor: NotRequired[str]
    vendor_display_name: NotRequired[str]
    source_url: NotRequired[Optional[str]]
    source_urls: NotRequired[List[Dict[str, str]]]
    participation: NotRequired[Optional[Dict[str, Any]]]
    cached: NotRequired[bool]


class SearchAmbiguousResponse(TypedDict):
    """Response when multiple cities match (user must select one)."""
    success: Literal[False]
    message: str
    query: str
    type: str
    ambiguous: Literal[True]
    city_options: List[CityOption]
    meetings: List[Any]


SearchResponse = Union[SearchSuccessResponse, SearchNotFoundResponse, SearchAmbiguousResponse]



async def handle_zipcode_search(zipcode: str, db: Database) -> SearchResponse:
    """Handle zipcode search (cache-only). Returns city data with banana as URL path."""
    city = await db.get_city(zipcode=zipcode)
    if not city:
        return cast(SearchResponse, {
            "success": False,
            "message": "We're not covering that area yet, but we're always expanding! Thanks for your interest - we'll prioritize cities with high demand.",
            "query": zipcode,
            "type": "zipcode",
            "meetings": [],
        })

    meetings = await db.get_meetings(bananas=[city.banana], limit=50, exclude_cancelled=False)

    await _log_city_search(db, city.banana, zipcode)

    if meetings:
        logger.info("found cached meetings", count=len(meetings), city=city.name, state=city.state)
        meetings_with_items = await get_meetings_for_listing(meetings, db)
        return cast(SearchResponse, {
            "success": True,
            "city_name": city.name,
            "state": city.state,
            "banana": city.banana,
            "vendor": city.vendor,
            "vendor_display_name": get_vendor_display_name(city.vendor),
            "source_url": get_vendor_source_url(city.vendor, city.slug),
            "source_urls": get_vendor_source_urls(city.vendor, city.slug),
            "participation": city.participation,
            "meetings": meetings_with_items,
            "cached": True,
            "query": zipcode,
            "type": "zipcode",
        })

    return cast(SearchResponse, {
        "success": True,
        "city_name": city.name,
        "state": city.state,
        "banana": city.banana,
        "vendor": city.vendor,
        "vendor_display_name": get_vendor_display_name(city.vendor),
        "source_url": get_vendor_source_url(city.vendor, city.slug),
        "source_urls": get_vendor_source_urls(city.vendor, city.slug),
        "participation": city.participation,
        "meetings": [],
        "cached": False,
        "query": zipcode,
        "type": "zipcode",
        "message": f"No meetings available yet for {city.name} - check back soon as we sync with the city website",
    })


async def handle_city_search(city_input: str, db: Database) -> SearchResponse:
    """Handle city name search (cache-only). Returns city data with banana as URL path."""
    city_name, state = parse_city_state_input(city_input)

    if not state:
        return await handle_ambiguous_city_search(city_name, city_input, db)

    # Banana lookup handles compressed multi-word cities (e.g., "mountairy, NC" -> "mountairyNC")
    potential_banana = f"{city_name.lower().replace(' ', '')}{state.upper()}"
    city = await db.get_city(banana=potential_banana)
    if not city:
        city = await db.get_city(name=city_name, state=state)

    if not city:
        await _record_city_request(db, f"{city_name.lower().replace(' ', '')}{state.upper()}")
        return cast(SearchResponse, {
            "success": False,
            "message": f"We're not covering {city_name}, {state} yet, but we're always expanding! Your interest has been noted - we prioritize cities with high demand.",
            "query": city_input,
            "type": "city_name",
            "meetings": [],
        })

    meetings = await db.get_meetings(bananas=[city.banana], limit=50, exclude_cancelled=False)

    await _log_city_search(db, city.banana, city_input)

    if meetings:
        logger.info("found cached meetings for city", count=len(meetings), city=city_name, state=state)
        meetings_with_items = await get_meetings_for_listing(meetings, db)
        return cast(SearchResponse, {
            "success": True,
            "city_name": city.name,
            "state": city.state,
            "banana": city.banana,
            "vendor": city.vendor,
            "vendor_display_name": get_vendor_display_name(city.vendor),
            "source_url": get_vendor_source_url(city.vendor, city.slug),
            "source_urls": get_vendor_source_urls(city.vendor, city.slug),
            "participation": city.participation,
            "meetings": meetings_with_items,
            "cached": True,
            "query": city_input,
            "type": "city",
        })

    return cast(SearchResponse, {
        "success": True,
        "city_name": city.name,
        "state": city.state,
        "banana": city.banana,
        "vendor": city.vendor,
        "vendor_display_name": get_vendor_display_name(city.vendor),
        "source_url": get_vendor_source_url(city.vendor, city.slug),
        "source_urls": get_vendor_source_urls(city.vendor, city.slug),
        "participation": city.participation,
        "meetings": [],
        "cached": False,
        "query": city_input,
        "type": "city",
        "message": f"No meetings available yet for {city_name}, {state} - check back soon as we sync with the city website",
    })


async def handle_state_search(state_input: str, db: Database) -> SearchResponse:
    """Handle state search - return list of cities in that state."""
    state_abbr = get_state_abbreviation(state_input)
    state_full = get_state_full_name(state_input)

    if not state_abbr:
        return cast(SearchResponse, {
            "success": False,
            "message": f"'{state_input}' is not a recognized state.",
            "query": state_input,
            "type": "state",
            "meetings": [],
        })

    cities = await db.get_cities(state=state_abbr)
    if not cities:
        return cast(SearchResponse, {
            "success": False,
            "message": f"We don't have any cities in {state_full} yet, but we're always expanding!",
            "query": state_input,
            "type": "state",
            "meetings": [],
        })

    city_options = cast(
        List[CityOption],
        [
            {
                "city_name": city.name,
                "state": city.state,
                "banana": city.banana,
                "vendor": city.vendor,
                "display_name": f"{city.name}, {city.state}",
                "covered": True,
            }
            for city in cities
        ],
    )

    stats = await db.get_city_meeting_stats([city.banana for city in cities])
    _apply_stats_to_options(city_options, stats)

    response: SearchAmbiguousResponse = {
        "success": False,
        "message": f"Found {len(city_options)} cities in {state_full} -- [<span style='color: #64748b'>total</span> | <span style='color: #4f46e5'>with packet</span> | <span style='color: #10b981'>summarized</span>]\n\nSelect a city to view its meetings:",
        "query": state_input,
        "type": "state",
        "ambiguous": True,
        "city_options": city_options,
        "meetings": [],
    }
    return response


async def handle_ambiguous_city_search(
    city_name: str, original_input: str, db: Database
) -> SearchResponse:
    """Handle city search when no state provided.

    Checks uszipcode even with single DB match to avoid auto-selecting wrong city
    (e.g., Roswell GA vs Roswell NM).
    """
    logger.debug("ambiguous city search", city=city_name, query=original_input)

    cities = await db.get_cities(name=city_name)
    if cities:
        logger.debug("found in database", city=city_name, states=[c.state for c in cities], count=len(cities))

    # Fuzzy match if no exact match
    if not cities:
        cities = await _fuzzy_match_cities(city_name, db)

    # Not in DB at all - check uszipcode for state resolution
    if not cities:
        return await _handle_unknown_city(city_name, original_input, db)

    # Single DB match - check if other states exist
    if len(cities) == 1:
        return await _handle_single_city_match(cities[0], city_name, original_input, db)

    # Multiple DB matches
    return await _handle_multiple_city_matches(cities, city_name, original_input, db)


async def _fuzzy_match_cities(city_name: str, db: Database) -> List[Any]:
    """Try fuzzy matching for typos. Returns matched cities or empty list."""
    all_names = await db.get_city_names()
    close_matches = get_close_matches(city_name.lower(), [n.lower() for n in all_names], n=5, cutoff=0.8)

    if not close_matches:
        return []

    # Prefer exact case-insensitive match
    exact_match = next((m for m in close_matches if m.lower() == city_name.lower()), None)
    if exact_match:
        close_matches = [exact_match]

    fuzzy_cities = []
    for match in close_matches:
        matched_cities = await db.get_cities(name=match)
        fuzzy_cities.extend(matched_cities)

    if fuzzy_cities:
        logger.info("fuzzy match found", query=city_name, matches=[c.name for c in fuzzy_cities])

    return fuzzy_cities


async def _handle_unknown_city(
    city_name: str, original_input: str, db: Database
) -> SearchResponse:
    """Handle city not in our DB - resolve via uszipcode."""
    logger.info("city not in database, checking uszipcode", city=city_name)
    resolved_states = await db.get_states_for_city_name(city_name)

    if len(resolved_states) > 1:
        logger.info("uszipcode found multiple states", city=city_name, states=resolved_states[:5])
        city_options = [_make_uncovered_option(city_name, state) for state in resolved_states[:5]]
        ambiguous_response: SearchAmbiguousResponse = {
            "success": False,
            "message": f"'{city_name.title()}' exists in multiple states. Which one are you looking for?",
            "query": original_input,
            "type": "city",
            "ambiguous": True,
            "city_options": city_options,
            "meetings": [],
        }
        return ambiguous_response

    # Record demand
    if resolved_states:
        requested_banana = f"{city_name.lower().replace(' ', '')}{resolved_states[0]}"
        logger.info("uszipcode resolved single state", city=city_name, state=resolved_states[0])
        message = f"We don't have '{city_name.title()}, {resolved_states[0]}' yet - your interest has been noted!"
    else:
        requested_banana = f"{city_name.lower().replace(' ', '')}UNKNOWN"
        logger.info("uszipcode found no match", city=city_name)
        message = f"We don't have '{city_name}' in our database. Please include the state (e.g., '{city_name}, CA') - your interest has been noted!"

    await _record_city_request(db, requested_banana)

    not_found_response: SearchNotFoundResponse = {
        "success": False,
        "message": message,
        "query": original_input,
        "type": "city",
        "meetings": [],
        "ambiguous": False,
    }
    return not_found_response


async def _handle_single_city_match(
    city: Any, city_name: str, original_input: str, db: Database
) -> SearchResponse:
    """Handle single DB match - return immediately if has meetings, else disambiguate."""
    # If we have meetings, return immediately (Boston MA with data beats Boston GA we don't cover)
    meetings = await db.get_meetings(bananas=[city.banana], limit=50, exclude_cancelled=False)

    if meetings:
        await _log_city_search(db, city.banana, original_input)
        logger.info("found cached meetings", count=len(meetings), city=city.name, state=city.state)
        meetings_with_items = await get_meetings_for_listing(meetings, db)
        success_response: SearchSuccessResponse = {
            "success": True,
            "city_name": city.name,
            "state": city.state,
            "banana": city.banana,
            "vendor": city.vendor,
            "vendor_display_name": get_vendor_display_name(city.vendor),
            "source_url": get_vendor_source_url(city.vendor, city.slug),
            "source_urls": get_vendor_source_urls(city.vendor, city.slug),
            "participation": city.participation,
            "meetings": meetings_with_items,
            "cached": True,
            "query": original_input,
            "type": "city",
            "ambiguous": False,
        }
        return success_response

    # No meetings - check if other states exist for disambiguation
    other_states = [s for s in await db.get_states_for_city_name(city_name) if s != city.state]

    if other_states:
        logger.info("no meetings, disambiguating with other states", city=city_name, covered=city.state, uncovered=other_states)
        covered_option = cast(
            CityOption,
            {
                "city_name": city.name,
                "state": city.state,
                "banana": city.banana,
                "vendor": city.vendor,
                "display_name": f"{city.name}, {city.state}",
                "covered": True,
            },
        )
        city_options = [covered_option] + [
            _make_uncovered_option(city_name, state) for state in other_states[:4]
        ]

        stats = await db.get_city_meeting_stats([city.banana])
        _apply_stats_to_options(city_options[:1], stats)

        ambiguous_response: SearchAmbiguousResponse = {
            "success": False,
            "message": f"Multiple cities named '{city_name.title()}' exist. Which one are you looking for?",
            "query": original_input,
            "type": "city",
            "ambiguous": True,
            "city_options": city_options,
            "meetings": [],
        }
        return ambiguous_response

    # No meetings and no other states
    not_found_response: SearchNotFoundResponse = {
        "success": False,
        "city_name": city.name,
        "state": city.state,
        "banana": city.banana,
        "vendor": city.vendor,
        "vendor_display_name": get_vendor_display_name(city.vendor),
        "source_url": get_vendor_source_url(city.vendor, city.slug),
        "source_urls": get_vendor_source_urls(city.vendor, city.slug),
        "participation": city.participation,
        "meetings": [],
        "cached": True,
        "query": original_input,
        "type": "city",
        "message": f"No meetings cached yet for {city.name}, {city.state}, please check back soon!",
        "ambiguous": False,
    }
    return not_found_response


async def _handle_multiple_city_matches(
    cities: List[Any], city_name: str, original_input: str, db: Database
) -> SearchResponse:
    """Handle multiple DB matches - return ambiguous result."""
    logger.info("multiple db matches", city=city_name, count=len(cities), states=[c.state for c in cities])

    city_options = cast(
        List[CityOption],
        [
            {
                "city_name": city.name,
                "state": city.state,
                "banana": city.banana,
                "vendor": city.vendor,
                "display_name": f"{city.name}, {city.state}",
                "covered": True,
            }
            for city in cities
        ],
    )

    stats = await db.get_city_meeting_stats([city.banana for city in cities])
    _apply_stats_to_options(city_options, stats)

    response: SearchAmbiguousResponse = {
        "success": False,
        "message": f"Multiple cities named '{city_name.title()}' found. Which one are you looking for?",
        "query": original_input,
        "type": "city",
        "ambiguous": True,
        "city_options": city_options,
        "meetings": [],
    }
    return response
