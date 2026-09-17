# Custom City Adapters

## Overview

Custom adapters are 1:1 with specific high-value cities that don't use standard civic tech vendor platforms. Unlike vendor adapters (1:many), these require custom HTML parsing for each city.

**Trade-off**: High maintenance cost per city, but captures high-value targets (Berkeley, Menlo Park, etc.) that wouldn't otherwise be accessible.

## When to Create a Custom Adapter

✅ **Create custom adapter when:**
- City has high value (large population, important decisions, user demand)
- City has structured HTML that can be parsed reliably
- Maintenance burden is justified by city importance
- No vendor adapter available or planned

❌ **Don't create custom adapter when:**
- City has <50k population and low civic engagement
- HTML structure is too fragile or JavaScript-rendered
- City already uses a supported vendor platform
- Better investment is supporting a new vendor (reaches multiple cities)

## Target Cities

**Implemented:**
- Berkeley, CA (118k population) - Drupal CMS, item-level extraction
- Chicago, IL (2.7M population) - REST API, concurrent matter fetches
- Menlo Park, CA (35k population) - Simple table, monolithic processing
- Ross, CA (2.5k population) - AHA FastTrack/Drupal 7, structured staff report items from detail pages

**Potential Future:**
- Honolulu, HI (350k) - City Council meetings at https://hnldoc.ehawaii.gov/hnldoc/videos. Has agendas (`/hnldoc/agenda/`), YouTube video links, and minutes (`/hnldoc/document-download/`). No API/RSS — would need HTML scraping. Year filter covers 2019-2027. Committees: Budget, Zoning & Planning, Energy, Infrastructure, etc.
- San Francisco, CA (815k) - High value if feasible
- Oakland, CA (440k) - High value if feasible
- Mountain View, CA (82k) - If table structure is similar to Menlo Park

**Threshold**: Generally target cities >30k population or with exceptionally high civic engagement.

## Implementation Pattern

### 1. Create adapter file

```python
# vendors/adapters/custom/cityname_adapter_async.py

from vendors.adapters.base_adapter_async import AsyncBaseAdapter, logger

class AsyncCityNameAdapter(AsyncBaseAdapter):
    """City Name - Custom [CMS type] adapter (async)

    URL patterns:
    - Meetings list: https://city.gov/meetings
    - Detail page: https://city.gov/meeting/2025-11-10

    HTML structure:
    - [Document key selectors and patterns]

    Confidence: X/10 - [Note concerns]
    """

    def __init__(self, city_slug: str):
        super().__init__(city_slug, vendor="cityname")
        self.base_url = "https://city.gov"

    async def fetch_meetings(self, days_back: int = 28, days_forward: int = 28):
        """Fetch and parse meetings (async)"""
        response = await self._get(f"{self.base_url}/meetings")
        html = await response.text()
        # Parse with asyncio.to_thread for CPU-bound work
        soup = await asyncio.to_thread(BeautifulSoup, html, 'html.parser')
        # Extract meetings
        pass
```

### 2. Update factory.py

```python
# Add import
from vendors.adapters.custom.cityname_adapter_async import AsyncCityNameAdapter

# Add to VENDOR_ADAPTERS dict
VENDOR_ADAPTERS = {
    ...
    "cityname": AsyncCityNameAdapter,
}
```

### 3. Update custom/__init__.py

```python
from vendors.adapters.custom.cityname_adapter_async import AsyncCityNameAdapter

__all__ = [..., "AsyncCityNameAdapter"]
```

### 4. Add city to database

```sql
INSERT INTO cities (city_banana, name, state, vendor, city_slug, ...)
VALUES ('citynameCA', 'City Name', 'CA', 'cityname', 'cityname', ...);
```

## Common Patterns

### Date Parsing

Use base adapter's utility or implement custom:

```python
def _parse_date(self, date_str: str) -> datetime:
    """Parse city-specific date formats"""
    for fmt in ["%b. %d, %Y", "%B %d, %Y", "%m/%d/%Y"]:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None
```

### Item Extraction

Look for numbered or lettered items:

```python
# Numbered: 1., 2., 3.
item_match = re.match(r'^(\d+)\.$', text)

# Lettered: A1., B2., C3.
item_match = re.match(r'^([A-Z])(\d+)\.$', text)
```

### Participation Info

Reuse parsing patterns from `html_agenda_parser.py`:

```python
# Email
email_match = re.search(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}', text)

# Zoom
zoom_match = re.search(r'https://[^/]*zoom[^/]*/j/(\d+)', text)

# Phone
phone_match = re.search(r'1-(\d{3})-(\d{3})-(\d{4})', text)
```

## Maintenance Considerations

**Each custom adapter requires:**
- Initial development: 4-6 hours
- Testing and refinement: 2-4 hours
- Ongoing maintenance: ~1 hour per quarter (handle HTML changes)
- Monitoring: Check success rate monthly

**When to deprecate:**
- City adopts a supported vendor platform → migrate to vendor adapter
- HTML structure becomes too fragile → mark as unsupported
- City population/engagement drops → not worth maintenance burden

**Documentation:**
- Document HTML structure in adapter docstring
- Note confidence level (1-10 scale)
- Add TODO comments for uncertain patterns
- Update this README when adding/removing adapters

## Testing

Before deploying custom adapter:

1. **Fetch test**: Verify meetings list URL and parsing
2. **Detail test**: Check item extraction and participation info
3. **Date range test**: Ensure future and past meetings parse correctly
4. **Edge cases**: Test special meetings, cancelled meetings, empty agendas

```bash
# Manual test in Python REPL
from vendors.factory import get_async_adapter
adapter = get_async_adapter("berkeley", "berkeley")

# Run async fetch
import asyncio
meetings = asyncio.run(adapter.fetch_meetings(max_meetings=3))
print(meetings)
```

## Success Metrics

Track per adapter:
- **Fetch success rate**: % of attempts that return meetings
- **Item extraction rate**: % of meetings with items extracted
- **Average items per meeting**: Quality check (expect 5-20 for most councils)
- **Failure patterns**: Common HTML changes that break parsing

Target: >90% success rate or deprecate adapter.

---

**Last Updated**: 2026-03-25
**Active Adapters**: 4 (Berkeley, Chicago, Menlo Park, Ross)
**Total Cities Covered**: 4
**Architecture**: All async (migration complete Nov 2025)
