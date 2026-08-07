# Database - Persistence & Repository Pattern

**Purpose:** Single source of truth for all Engagic data. PostgreSQL with async connection pooling.

Manages:
- Jurisdictions (cities, counties, transit authorities, utilities), zipcodes, and census data
- Meetings and agenda items
- Matters (legislative tracking across meetings)
- Council members, sponsorships, and votes
- Committees and memberships
- Processing queue (typed jobs)
- Full-text search, topics, and caching
- User engagement (watches, activity, trending)
- User feedback (ratings, issues)
- User authentication, alerts, and notifications (userland schema)
- Deliberation (citizen comment forums with opinion clustering)
- "Happening This Week" (AI-curated important items)
- Session analytics (anonymous journey tracking)

---

## Architecture Overview

**Repository Pattern** with async PostgreSQL (asyncpg connection pooling):

```
┌─────────────────────┐
│  Database           │  Orchestration + Facade (419 lines)
│  (db_postgres.py)   │
└──────────┬──────────┘
           │
           ├──> repositories_async/base.py            (129 lines) - Base repository with connection pooling
           ├──> repositories_async/cities.py          (366 lines) - Jurisdiction and zipcode operations
           ├──> repositories_async/meetings.py        (290 lines) - Meeting storage and retrieval
           ├──> repositories_async/items.py           (500 lines) - Agenda item operations
           ├──> repositories_async/matters.py         (555 lines) - Matter operations (matters-first)
           ├──> repositories_async/queue.py           (460 lines) - Processing queue management
           ├──> repositories_async/search.py          (259 lines) - PostgreSQL full-text search
           ├──> repositories_async/userland.py        (794 lines) - User auth, alerts, refresh tokens
           ├──> repositories_async/council_members.py (761 lines) - Council member tracking, sponsorships, votes
           ├──> repositories_async/committees.py      (598 lines) - Committee management, memberships
           ├──> repositories_async/engagement.py      (176 lines) - Watches, activity logging, trending
           ├──> repositories_async/feedback.py        (343 lines) - Ratings, issue reporting
           ├──> repositories_async/helpers.py         (185 lines) - Shared builders, JSONB deserialization
           ├──> repositories_async/deliberation.py    (757 lines) - Deliberation comments, votes, clustering
           └──> repositories_async/happening.py       (125 lines) - "Happening This Week" curated items

Supporting Modules:
├── models.py          (483 lines) - Pydantic dataclasses + JSONB models
├── id_generation.py   (721 lines) - Deterministic ID generation (meetings, items, matters, members, committees)
├── vote_utils.py      (47 lines)  - Vote tally computation and outcome determination
├── migrate.py         (271 lines) - Versioned SQL migration runner
├── schema_postgres.sql - Main schema (jurisdictions, meetings, items, matters, queue, council_members, committees, votes, deliberations, happening_items, session_events, tenants)
└── schema_userland.sql - Userland schema (users, alerts, alert_matches, refresh_tokens, city_requests, watches, ratings, issues, deliberation_trusted_users)
```

**Total: ~8,300 lines** (419 orchestration + 6,300 repositories + 1,500 supporting + 29 init)

**Why Repository Pattern?**
- **Separation of concerns:** Each repository handles one domain
- **Testability:** Mock repositories independently
- **Maintainability:** Focused modules > monolithic database layer
- **Shared connection pool:** asyncpg pool (5-20 connections) across all repositories
- **True concurrency:** PostgreSQL handles concurrent operations safely

---

## Quick Start

### Basic Usage

```python
from database.db_postgres import Database

# Create database with connection pool
db = await Database.create()

# --- Facade methods (convenience wrappers) ---

# City operations (facade)
city = await db.get_city(banana="paloaltoCA")
city = await db.get_city(zipcode="94301")
city = await db.get_city(name="Palo Alto", state="CA")
cities = await db.get_cities(state="CA", vendor="primegov")

# Meeting operations (facade)
meeting = await db.get_meeting("paloaltoCA_a3f2c8d1")
meetings = await db.get_meetings(bananas=["paloaltoCA", "oaklandCA"], limit=50)

# Agenda items with optional matter loading (facade)
items = await db.get_agenda_items("paloaltoCA_a3f2c8d1", load_matters=True)
items_map = await db.get_items_for_meetings(["meeting_1", "meeting_2"], load_matters=True)

# Search (facade)
meetings = await db.search_meetings_by_topic("housing", city_banana="paloaltoCA")
topics = await db.get_popular_topics(limit=20)

# Stats (facade)
stats = await db.get_stats()
metrics = await db.get_platform_metrics()

# --- Direct repository access ---

# Queue
# Domain writers normally publish through pipeline_lifecycle.enqueue_queue_job()
# on their transaction connection. Direct enqueue is for dispatch/retry tooling.
await db.queue.enqueue_job(
    source_url, job_type="meeting", payload={...}, priority=150,
    work_version="mv1:...",
    processing_metadata={"chunk": audit},  # optional diagnostic trail (chunker cascade audit)
)
job = await db.queue.get_next_for_processing()           # any job (legacy)
job = await db.queue.get_next_for_processing(lane="streaming")  # urgent-window meetings, matters, undated
job = await db.queue.get_next_for_processing(lane="batch")      # non-urgent meetings -> Gemini Batch lane
await db.queue.heartbeat_job(job.id, job.claim_token, job.work_version)
await db.queue.mark_processing_complete(job.id, job.claim_token, job.work_version)
hints = await db.queue.get_chunker_hints()  # latest winning chunker rung per (vendor, slug, ladder)

# Council members
member = await db.council_members.find_or_create_member("nashvilleTN", "Freddie O'Connell")
await db.council_members.record_vote(member.id, matter_id, meeting_id, "yes")

# Deliberation
delib = await db.deliberation.create_deliberation(matter_id, "nashvilleTN")

# Cleanup
await db.close()
```

---

## Database Schema

### Core Tables

#### `jurisdictions` - Jurisdiction Registry
```sql
CREATE TABLE jurisdictions (
    banana TEXT PRIMARY KEY,           -- paloaltoCA (vendor-agnostic)
    name TEXT NOT NULL,                -- Palo Alto
    state TEXT NOT NULL,               -- CA
    vendor TEXT NOT NULL,              -- primegov, legistar, granicus
    slug TEXT NOT NULL,                -- cityofpaloalto (vendor-specific)
    type TEXT DEFAULT 'city',          -- city, county, transit, utility, water, school
    county_banana TEXT,                -- FK to parent jurisdiction (self-referencing)
    status TEXT DEFAULT 'active',      -- active, inactive
    participation JSONB,               -- Jurisdiction-level participation config: {testimony_url, testimony_email, process_url}
    population INTEGER,                -- Census 2020 population
    geom geometry(MultiPolygon, 4326), -- Boundary from Census TIGER/Line (PostGIS)
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    UNIQUE(name, state),
    FOREIGN KEY (county_banana) REFERENCES jurisdictions(banana)
);
```

**Jurisdictions** (formerly `cities`): Supports any public entity publishing agendas - cities, counties, transit authorities, utility districts, water districts, school boards, etc. The `type` column enables filtering and different handling per jurisdiction type. The `county_banana` self-referencing FK allows hierarchical relationships (cities reference their parent county).

**PostGIS:** Boundaries stored as MultiPolygon geometries for map visualization. Requires `postgis` extension.

**Backward Compatibility:** `City` is an alias for `Jurisdiction` in `models.py`.

#### `zipcodes` - Jurisdiction Zipcodes (Many-to-Many)
```sql
CREATE TABLE zipcodes (
    banana TEXT NOT NULL,
    zipcode TEXT NOT NULL,
    is_primary BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (banana, zipcode),
    FOREIGN KEY (banana) REFERENCES jurisdictions(banana) ON DELETE CASCADE
);
```

#### `meetings` - Meeting Data
```sql
CREATE TABLE meetings (
    id TEXT PRIMARY KEY,               -- paloaltoCA_a3f2c8d1 (banana + 8-char MD5)
    banana TEXT NOT NULL,
    title TEXT NOT NULL,
    date TIMESTAMP,
    agenda_url TEXT,                   -- HTML agenda (item-based, primary)
    agenda_sources JSONB,              -- [{type, url, label}] for multi-agenda provenance
    packet_url TEXT,                   -- PDF packet (monolithic, fallback)
    summary TEXT,                      -- LLM-generated summary (optional)
    participation JSONB,               -- {email, phone, virtual_url, streaming_urls, ...}
    status TEXT,                       -- cancelled, postponed, revised, rescheduled, or NULL
    processing_status TEXT DEFAULT 'pending',  -- pending, processing, completed, failed
    processing_method TEXT,            -- tier1_pypdf2_gemini, item_level_N_items
    processing_time REAL,
    committee_id TEXT,                 -- FK to committees (meeting is occurrence of committee)
    search_vector tsvector GENERATED,  -- Stored FTS column
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    FOREIGN KEY (banana) REFERENCES jurisdictions(banana) ON DELETE CASCADE,
    FOREIGN KEY (committee_id) REFERENCES committees(id) ON DELETE SET NULL
);
```

**URL Architecture:**
- **`agenda_url`** - HTML page (item-based meetings)
- **`packet_url`** - PDF file (monolithic meetings)

Topics stored in normalized `meeting_topics` table (not JSON column).

#### `items` - Agenda Items
```sql
CREATE TABLE items (
    id TEXT PRIMARY KEY,               -- paloaltoCA_a3f2c8d1_ord2024-123
    meeting_id TEXT NOT NULL,
    title TEXT NOT NULL,
    sequence INTEGER NOT NULL,         -- Order in agenda
    attachments JSONB,                 -- [{url, name, type}]
    attachment_hash TEXT,              -- SHA-256 for change detection
    matter_id TEXT,                    -- FK to city_matters.id (composite hash)
    matter_file TEXT,                  -- Public ID (BL2025-1005, 25-1209)
    matter_type TEXT,                  -- Ordinance, Resolution, etc.
    agenda_number TEXT,                -- Position on this agenda (1, K. 87)
    sponsors JSONB,                    -- ["Jane Doe", "John Smith"]
    summary TEXT,
    topics JSONB,                      -- Normalized to item_topics table
    quality_score REAL,                -- Denormalized from ratings
    rating_count INTEGER DEFAULT 0,
    search_vector tsvector GENERATED,
    created_at TIMESTAMP,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE,
    FOREIGN KEY (matter_id) REFERENCES city_matters(id) ON DELETE SET NULL
);
```

#### `city_matters` - Matter Registry (Matters-First Architecture)
```sql
CREATE TABLE city_matters (
    id TEXT PRIMARY KEY,               -- nashvilleTN_7a8f3b2c1d9e4f5a (banana + 16-char SHA256)
    banana TEXT NOT NULL,
    matter_file TEXT,                  -- Stable public file number (BL2025-1098)
    matter_id TEXT,                    -- Vendor-specific UUID (may be unstable)
    matter_type TEXT,
    title TEXT NOT NULL,
    sponsors JSONB,
    canonical_summary TEXT,            -- Deduplicated summary (stored once)
    canonical_topics JSONB,
    attachments JSONB,
    metadata JSONB,                    -- {attachment_hash, ...}
    first_seen TIMESTAMP,
    last_seen TIMESTAMP,
    appearance_count INTEGER DEFAULT 1,
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'passed', 'failed', 'tabled', 'withdrawn', 'referred', 'amended', 'vetoed', 'enacted')),
    final_vote_date TIMESTAMP,         -- Terminal vote date
    quality_score REAL,
    rating_count INTEGER DEFAULT 0,
    search_vector tsvector GENERATED,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    FOREIGN KEY (banana) REFERENCES jurisdictions(banana) ON DELETE CASCADE
);
```

**Matters-First Architecture:**
- **Deduplicate summarization:** Matter processed once, reused across meetings
- **Attachment hash:** Detect changes (re-process only if attachments change)
- **Status tracking:** Full lifecycle from `active` through `passed`/`failed`/`enacted`
- **Timeline tracking:** `first_seen`, `last_seen`, `appearance_count`

#### `matter_appearances` - Matter Timeline
```sql
CREATE TABLE matter_appearances (
    id BIGSERIAL PRIMARY KEY,
    matter_id TEXT NOT NULL,
    meeting_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    appeared_at TIMESTAMP,              -- NULL when the source meeting is undated
    committee TEXT,                    -- Committee name (text, for display)
    action TEXT,                       -- Action taken at this appearance
    vote_outcome TEXT CHECK (... IN ('passed', 'failed', 'tabled', 'withdrawn', 'referred', 'amended', 'unknown', 'no_vote')),
    vote_tally JSONB,                 -- {yes: N, no: N, abstain: N, absent: N}
    committee_id TEXT,                -- FK to committees for relational queries
    sequence INTEGER,
    UNIQUE(matter_id, meeting_id, item_id),
    FOREIGN KEY (matter_id) REFERENCES city_matters(id) ON DELETE CASCADE,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE,
    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE,
    FOREIGN KEY (committee_id) REFERENCES committees(id) ON DELETE SET NULL
);
```

#### `queue` - Processing Queue (Typed Jobs)
```sql
CREATE TABLE queue (
    id BIGSERIAL PRIMARY KEY,
    source_url TEXT NOT NULL UNIQUE,   -- Deduplication key
    meeting_id TEXT,
    banana TEXT,
    job_type TEXT,                     -- "meeting" or "matter"
    payload JSONB,                     -- MeetingJob or MatterJob
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'dead_letter')),
    priority INTEGER DEFAULT 0,        -- Higher = processed first
    retry_count INTEGER DEFAULT 0,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    last_enqueued_at TIMESTAMP,
    retry_at TIMESTAMP,
    ready_at TIMESTAMP NOT NULL,
    started_at TIMESTAMP,
    claim_token UUID,
    claimed_at TIMESTAMP,
    heartbeat_at TIMESTAMP,
    completed_at TIMESTAMP,
    failed_at TIMESTAMP,
    error_message TEXT,
    processing_metadata JSONB,
    work_version TEXT,
    desired_generation BIGINT NOT NULL,
    FOREIGN KEY (banana) REFERENCES jurisdictions(banana) ON DELETE CASCADE,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);
```

#### Normalized Topic Tables
```sql
-- Topics stored relationally (not JSON arrays) for efficient indexing
CREATE TABLE meeting_topics (meeting_id TEXT, topic TEXT, PRIMARY KEY (meeting_id, topic));
CREATE TABLE item_topics    (item_id TEXT,    topic TEXT, PRIMARY KEY (item_id, topic));
CREATE TABLE matter_topics  (matter_id TEXT,  topic TEXT, PRIMARY KEY (matter_id, topic));
```

#### `cache` - Processing Cache
```sql
CREATE TABLE cache (
    packet_url TEXT PRIMARY KEY,
    content_hash TEXT,
    processing_method TEXT,
    processing_time REAL,
    cache_hit_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

#### `happening_items` - AI-Curated Important Items
```sql
CREATE TABLE happening_items (
    id SERIAL PRIMARY KEY,
    banana TEXT NOT NULL,
    item_id TEXT NOT NULL,
    meeting_id TEXT NOT NULL,
    meeting_date TIMESTAMP NOT NULL,
    rank INTEGER NOT NULL,             -- 1 = most important
    reason TEXT NOT NULL,              -- One-sentence explanation
    created_at TIMESTAMP DEFAULT NOW(),
    expires_at TIMESTAMP NOT NULL,     -- Usually meeting_date + 1 day
    UNIQUE(banana, item_id)
);
```

#### `session_events` - Anonymous Journey Tracking
```sql
CREATE TABLE session_events (
    id SERIAL PRIMARY KEY,
    ip_hash TEXT NOT NULL,             -- SHA256[:16] of client IP
    event TEXT NOT NULL,
    url TEXT,
    properties JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
-- 7-day retention via cleanup_old_session_events() function
```

### Council Members & Voting Tables

#### `council_members` - Elected Officials Registry
```sql
CREATE TABLE council_members (
    id TEXT PRIMARY KEY,               -- chicagoIL_cm_7a8f3b2c1d9e4f5a
    banana TEXT NOT NULL,
    name TEXT NOT NULL,                -- Display name
    normalized_name TEXT NOT NULL,     -- Lowercase for matching
    title TEXT,                        -- Council Member, Mayor, Alderman
    district TEXT,
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'former', 'unknown')),
    first_seen TIMESTAMP,
    last_seen TIMESTAMP,
    sponsorship_count INTEGER DEFAULT 0,
    vote_count INTEGER DEFAULT 0,
    metadata JSONB,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    UNIQUE(banana, normalized_name)
);
```

#### `sponsorships` - Member-Matter Links
```sql
CREATE TABLE sponsorships (
    id BIGSERIAL PRIMARY KEY,
    council_member_id TEXT NOT NULL,
    matter_id TEXT NOT NULL,
    is_primary BOOLEAN DEFAULT FALSE,
    sponsor_order INTEGER,
    UNIQUE(council_member_id, matter_id)
);
```

#### `votes` - Individual Voting Records
```sql
CREATE TABLE votes (
    id BIGSERIAL PRIMARY KEY,
    council_member_id TEXT NOT NULL,
    matter_id TEXT NOT NULL,
    meeting_id TEXT NOT NULL,
    vote TEXT NOT NULL CHECK (vote IN ('yes', 'no', 'abstain', 'absent', 'present', 'recused', 'not_voting')),
    vote_date TIMESTAMP,
    sequence INTEGER,
    metadata JSONB,
    UNIQUE(council_member_id, matter_id, meeting_id)
);
```

### Committee Tables

#### `committees` - Committee Registry
```sql
CREATE TABLE committees (
    id TEXT PRIMARY KEY,               -- chicagoIL_comm_7a8f3b2c1d9e4f5a
    banana TEXT NOT NULL,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'unknown')),
    UNIQUE(banana, normalized_name)
);
```

#### `committee_members` - Roster Tracking
```sql
CREATE TABLE committee_members (
    id BIGSERIAL PRIMARY KEY,
    committee_id TEXT NOT NULL,
    council_member_id TEXT NOT NULL,
    role TEXT,                         -- Chair, Vice-Chair, Member
    joined_at TIMESTAMP,
    left_at TIMESTAMP,                -- NULL = currently serving
    UNIQUE(committee_id, council_member_id, joined_at)
);
```

### Deliberation Tables

```sql
-- Deliberation sessions linked to matters
CREATE TABLE deliberations (
    id TEXT PRIMARY KEY,               -- delib_{matter_id}_{short_hash}
    matter_id TEXT NOT NULL,
    banana TEXT NOT NULL,
    topic TEXT,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ
);

-- Pseudonymous participant tracking
CREATE TABLE deliberation_participants (
    deliberation_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    participant_number INTEGER NOT NULL,  -- Displayed as "Participant 1", etc.
    PRIMARY KEY (deliberation_id, user_id)
);

-- User-submitted comments
CREATE TABLE deliberation_comments (
    id SERIAL PRIMARY KEY,
    deliberation_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    participant_number INTEGER NOT NULL,
    txt TEXT NOT NULL,
    mod_status INTEGER DEFAULT 0,      -- -1=hidden, 0=pending, 1=approved
    UNIQUE (deliberation_id, user_id, txt)
);

-- Votes on comments
CREATE TABLE deliberation_votes (
    comment_id INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    vote SMALLINT NOT NULL CHECK (vote IN (-1, 0, 1)),  -- -1=disagree, 0=pass, 1=agree
    PRIMARY KEY (comment_id, user_id)
);

-- Cached clustering results
CREATE TABLE deliberation_results (
    deliberation_id TEXT PRIMARY KEY,
    n_participants INTEGER,
    n_comments INTEGER,
    k INTEGER,                         -- Number of clusters
    positions JSONB,                   -- [[x,y], ...] per participant
    clusters JSONB,                    -- {user_id: cluster_id}
    cluster_centers JSONB,
    consensus JSONB,
    group_votes JSONB,
    computed_at TIMESTAMPTZ
);
```

**Trust-Based Moderation Flow:**
1. New user submits comment -> mod_status=0 (pending)
2. Moderator approves -> mod_status=1, user added to `userland.deliberation_trusted_users`
3. Trusted user submits comment -> mod_status=1 (auto-approved)

### Userland Schema

**Separate `userland` namespace for user-facing features:**

```sql
CREATE SCHEMA userland;

-- User accounts (email-based, magic link auth)
CREATE TABLE userland.users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    created_at TIMESTAMP,
    last_login TIMESTAMP
);

-- User-configured alerts
CREATE TABLE userland.alerts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    cities JSONB NOT NULL,             -- ["paloaltoCA", "mountainviewCA"]
    criteria JSONB NOT NULL,           -- {"keywords": ["housing", "zoning"]}
    frequency TEXT DEFAULT 'weekly' CHECK (frequency IN ('weekly', 'daily')),
    active BOOLEAN DEFAULT TRUE
);

-- Alert matches (triggered notifications)
CREATE TABLE userland.alert_matches (
    id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL,
    meeting_id TEXT NOT NULL,
    item_id TEXT,                      -- NULL for meeting-level matches
    match_type TEXT NOT NULL CHECK (match_type IN ('keyword', 'matter')),
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    matched_criteria JSONB NOT NULL,
    notified BOOLEAN DEFAULT FALSE
);

-- Magic link replay prevention
CREATE TABLE userland.used_magic_links (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL
);

-- Refresh token storage (for revocation support)
CREATE TABLE userland.refresh_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    revoked_reason TEXT
);

-- City requests (track user demand for new cities)
CREATE TABLE userland.city_requests (
    city_banana TEXT PRIMARY KEY,
    request_count INTEGER DEFAULT 1,
    first_requested TIMESTAMP,
    last_requested TIMESTAMP,
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'added', 'rejected'))
);

-- User watches (matters, meetings, topics, cities, council_members)
CREATE TABLE userland.watches (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('matter', 'meeting', 'topic', 'city', 'council_member')),
    entity_id TEXT NOT NULL,
    UNIQUE(user_id, entity_type, entity_id)
);

-- Activity log (views, watches, searches, shares, rates, reports)
CREATE TABLE userland.activity_log (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT,                      -- NULL for anonymous
    session_id TEXT,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    metadata JSONB
);

-- Ratings (1-5 stars on items, meetings, matters)
CREATE TABLE userland.ratings (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT,
    session_id TEXT,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('item', 'meeting', 'matter')),
    entity_id TEXT NOT NULL,
    rating SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    CONSTRAINT ratings_unique_user UNIQUE(user_id, entity_type, entity_id)
);

-- Issue reports (inaccurate, incomplete, misleading, offensive)
CREATE TABLE userland.issues (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT,
    session_id TEXT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    issue_type TEXT NOT NULL CHECK (issue_type IN ('inaccurate', 'incomplete', 'misleading', 'offensive', 'other')),
    description TEXT NOT NULL,
    status TEXT DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'dismissed')),
    admin_notes TEXT,
    resolved_at TIMESTAMP
);

-- Trending matters (materialized view, refreshed periodically)
CREATE MATERIALIZED VIEW userland.trending_matters AS
SELECT entity_id AS matter_id, COUNT(*) AS engagement, ...
FROM userland.activity_log WHERE entity_type = 'matter' AND created_at > NOW() - '7 days'
GROUP BY entity_id ORDER BY engagement DESC LIMIT 100;

-- Deliberation trust tracking
CREATE TABLE userland.deliberation_trusted_users (
    user_id TEXT PRIMARY KEY,
    first_approved_at TIMESTAMPTZ DEFAULT NOW()
);
```

### B2B Tables (Phase 5)

```sql
-- Tenants and their coverage/keyword preferences
CREATE TABLE tenants (id TEXT PRIMARY KEY, name TEXT, api_key TEXT UNIQUE, webhook_url TEXT);
CREATE TABLE tenant_coverage (tenant_id TEXT, banana TEXT, PRIMARY KEY (tenant_id, banana));
CREATE TABLE tenant_keywords (tenant_id TEXT, keyword TEXT, PRIMARY KEY (tenant_id, keyword));

-- Tracked items for tenant monitoring
CREATE TABLE tracked_items (id TEXT PRIMARY KEY, tenant_id TEXT, item_type TEXT, title TEXT, banana TEXT, status TEXT DEFAULT 'active');
CREATE TABLE tracked_item_meetings (tracked_item_id TEXT, meeting_id TEXT, excerpt TEXT, PRIMARY KEY (tracked_item_id, meeting_id));
```

---

## Data Models

All entities are **Pydantic dataclasses** with runtime validation and `.to_dict()` serialization.

### JSONB Pydantic Models

Typed models for JSONB column deserialization:

```python
from database.models import (
    ParticipationInfo,    # meetings.participation: {email, phone, virtual_url, streaming_urls, ...}
    CityParticipation,    # jurisdictions.participation: {testimony_url, testimony_email, process_url}
    AttachmentInfo,       # items/matters attachments: {name, url, type, history_id?}
    MatterMetadata,       # city_matters.metadata: {attachment_hash}
    EmailContext,         # Structured email: {address, purpose}
    StreamingUrl,         # Streaming link: {url, platform, channel?}
)
```

### Domain Dataclasses

```python
from database.models import Jurisdiction, City, Meeting, AgendaItem, Matter, CouncilMember, Vote, Committee, CommitteeMember

# Jurisdiction (13 fields) - City is a backward-compat alias for Jurisdiction
jurisdiction = Jurisdiction(
    banana="paloaltoCA",
    name="Palo Alto",
    state="CA",
    vendor="primegov",
    slug="cityofpaloalto",
    type="city",                # city, county, transit, utility, water, school
    county_banana="santaclaracountyCA",  # FK to parent jurisdiction
    population=68572,
    participation=CityParticipation(testimony_url="https://...")
)

# Meeting (17 fields)
meeting = Meeting(
    id="paloaltoCA_a3f2c8d1",   # banana + 8-char MD5
    banana="paloaltoCA",
    title="City Council - Regular Meeting",
    date=datetime(2025, 11, 10, 19, 0),
    agenda_url="https://...",
    committee_id="paloaltoCA_comm_b7d4e9f2a1c3d5e7"
)

# AgendaItem (17 fields)
item = AgendaItem(
    id="paloaltoCA_a3f2c8d1_ord2024-123",  # meeting_id + suffix
    meeting_id="paloaltoCA_a3f2c8d1",
    title="Approve Housing Project",
    sequence=5,
    matter_id="paloaltoCA_7a8f3b2c1d9e4f5a",  # FK to city_matters
    matter_file="25-1234",
    attachment_hash="abc123def456..."
)

# Matter (20 fields)
matter = Matter(
    id="nashvilleTN_7a8f3b2c1d9e4f5a",  # banana + 16-char SHA256
    banana="nashvilleTN",
    matter_file="BL2025-1098",
    matter_type="Ordinance",
    title="Housing Ordinance Amendment",
    canonical_summary="Summary text...",
    canonical_topics=["housing", "zoning"],
    status="active",  # active, passed, failed, tabled, withdrawn, referred, amended, vetoed, enacted
    metadata=MatterMetadata(attachment_hash="abc123")
)

# CouncilMember (14 fields)
member = CouncilMember(
    id="nashvilleTN_cm_a3f2c8d1e5f6g7h8",  # banana_cm_16-char-hash
    banana="nashvilleTN",
    name="Freddie O'Connell",
    normalized_name="freddie o'connell",
    title="Council Member",
    district="District 19"
)

# Vote (9 fields)
vote = Vote(
    council_member_id="nashvilleTN_cm_a3f2c8d1e5f6g7h8",
    matter_id="nashvilleTN_7a8f3b2c1d9e4f5a",
    meeting_id="nashvilleTN_b4c5d6e7",
    vote="yes"  # yes, no, abstain, absent, present, recused, not_voting
)

# Committee (8 fields)
committee = Committee(
    id="sanfranciscoCA_comm_b7d4e9f2a1c3d5e7",  # banana_comm_16-char-hash
    banana="sanfranciscoCA",
    name="Planning Commission",
    normalized_name="planning commission"
)

# CommitteeMember (7 fields)
assignment = CommitteeMember(
    committee_id="sanfranciscoCA_comm_b7d4e9f2a1c3d5e7",
    council_member_id="sanfranciscoCA_cm_c8e5f0a3b2d4e6f8",
    role="Chair",
    joined_at=datetime(2024, 1, 15),
    left_at=None  # Still serving
)
```

**Model Methods:**

```python
# Convert to dict (for JSON serialization)
city_dict = city.to_dict()   # Handles datetime → ISO strings, Pydantic → dicts
member_dict = member.to_dict()

# Models are constructed from DB rows via helpers:
from database.repositories_async.helpers import build_meeting, build_matter, build_agenda_item
```

**Runtime Validation:**
- `Meeting.__post_init__()` validates `processing_status` enum
- `Matter.__post_init__()` validates ID format via `validate_matter_id()`, requires banana, requires at least one identifier
- `AgendaItem.__post_init__()` validates `matter_id` format if present, non-negative sequence

---

## Repository Guide

### 1. JurisdictionRepository (366 lines)

**Methods:**

```python
# Get city by banana
city = await db.cities.get_city("paloaltoCA")

# Get city by zipcode
city = await db.cities.get_city_by_zipcode("94301")

# Batch lookup with filters
cities = await db.cities.get_cities(state="CA", vendor="primegov", status="active", include_zipcodes=True)

# All cities (lightweight)
cities = await db.cities.get_all_cities(status="active")

# City names only (for fuzzy matching)
names = await db.cities.get_city_names()

# Add / upsert city
await db.cities.add_city(city_obj)
await db.cities.upsert_city(city_obj)

# Meeting frequency and sync tracking
count = await db.cities.get_city_meeting_frequency("paloaltoCA", days=30)
last_sync = await db.cities.get_city_last_sync("paloaltoCA")
```

---

### 2. MeetingRepository (290 lines)

**Methods:**

```python
# Store meeting (upsert, preserves existing summary)
await db.meetings.store_meeting(meeting_obj)

# Get single meeting
meeting = await db.meetings.get_meeting("paloaltoCA_a3f2c8d1")

# Get meetings for city (ordered by date DESC)
meetings = await db.meetings.get_meetings_for_city("paloaltoCA", limit=50, offset=0)

# Get upcoming meetings
meetings = await db.meetings.get_upcoming_meetings("paloaltoCA", days=30)

# Batch lookup
meetings = await db.meetings.get_meetings_batch(["meeting_1", "meeting_2"])

# Date range query
meetings = await db.meetings.get_meetings_by_date_range("paloaltoCA", start_date, end_date)

# Keyword search
meetings = await db.meetings.search_by_keyword("paloaltoCA", "housing")

# Committee meetings
meetings = await db.meetings.get_meetings_by_committee(committee_id)

# Update status
await db.meetings.update_meeting_status(meeting_id, "completed")
```

**CRITICAL: Preservation on Conflict**

```sql
-- On conflict, PRESERVE existing summary if new value is NULL
ON CONFLICT(id) DO UPDATE SET
    summary = CASE
        WHEN excluded.summary IS NOT NULL THEN excluded.summary
        ELSE meetings.summary
    END
```

---

### 3. ItemRepository (500 lines)

**Methods:**

```python
# Store agenda items (batch, preserves summaries on re-sync)
await db.items.store_agenda_items(meeting_id, [item1, item2, item3])

# Get items for meeting (ordered by sequence)
items = await db.items.get_agenda_items("paloaltoCA_a3f2c8d1")

# Batch fetch for multiple meetings (eliminates N+1)
items_map = await db.items.get_items_for_meetings(["meeting_1", "meeting_2"])

# Get single item
item = await db.items.get_agenda_item(item_id)

# Update item summary and topics
await db.items.update_agenda_item(item_id, summary="...", topics=["housing"])

# Fill item summaries for unsnapshotted appearances (frozen-on-summary)
await db.items.bulk_fill_null_item_summaries(item_ids, summary, topics)

# Copy prior appearance's summary onto a new item when attachments unchanged
await db.items.copy_summary_from_prior_appearance(matter_id, target_item_id, target_meeting_id)

# Check which meetings have summarized items (lightweight for listings)
has_summaries = await db.items.get_has_summarized_items(meeting_ids)

# Get all items referencing a matter
items = await db.items.get_all_items_for_matter(matter_id)

# Topic and keyword search
items = await db.items.get_items_by_topic(meeting_id, "housing")
items = await db.items.search_by_keyword("paloaltoCA", "zoning", limit=50)
items = await db.items.search_upcoming_by_keyword("paloaltoCA", "zoning", days=30)

# Deduplication
deduped = await db.items.dedupe_items_by_matter(items)
```

---

### 4. MatterRepository (555 lines)

**Methods:**

```python
# Store matter (preserves canonical_summary on conflict)
await db.matters.store_matter(matter_obj)

# Get matter by composite ID
matter = await db.matters.get_matter("nashvilleTN_7a8f3b2c1d9e4f5a")

# Batch lookup
matters = await db.matters.get_matters_batch(["matter_1", "matter_2"])

# Update canonical summary
await db.matters.update_matter_summary(matter_id, summary="...", topics=["housing"])

# Update tracking (appearance count, final vote date)
await db.matters.update_matter_tracking(matter_id, appearance_count=5, final_vote_date=datetime.now())

# Update status
await db.matters.update_status(matter_id, "passed")

# Appearance tracking
exists = await db.matters.has_appearance(matter_id, meeting_id)
await db.matters.create_appearance(matter_id, meeting_id, item_id)
await db.matters.update_appearance_outcome(matter_id, meeting_id, item_id, vote_outcome="passed", vote_tally={...})

# Timeline view
timeline = await db.matters.get_timeline(matter_id)

# Get matter with voting details
matter = await db.matters.get_matter_with_votes(matter_id)
outcomes = await db.matters.get_matter_vote_outcomes(matter_id)

# Full-text and keyword search
results = await db.matters.search_matters_fulltext("affordable housing", banana="paloaltoCA")
results = await db.matters.search_by_keyword("paloaltoCA", "housing")

# Alert match checking
exists = await db.matters.check_existing_match(alert_id, matter_id)
```

---

### 5. QueueRepository

**Methods:**

```python
# Enqueue job
await db.queue.enqueue_job(
    source_url="https://...",
    job_type="meeting",
    payload={"meeting_id": "...", "source_url": "https://..."},
    meeting_id="paloaltoCA_a3f2c8d1",
    banana="paloaltoCA",
    priority=150,
    work_version="mv1:...",
)

# Read-only preview
jobs = await db.queue.preview_jobs(banana="paloaltoCA", limit=10)

# Atomic claim; lane is None, "streaming", or "batch"
job = await db.queue.get_next_for_processing(lane="streaming")

# Every heartbeat/final transition is fenced by claim token + work version
await db.queue.heartbeat_job(job.id, job.claim_token, job.work_version)
await db.queue.mark_processing_complete(
    job.id, job.claim_token, job.work_version
)
await db.queue.mark_processing_failed(
    job.id,
    "temporary error",
    claim_token=job.claim_token,
    work_version=job.work_version,
)

# Reset stuck jobs
count = await db.queue.reset_stale_processing_jobs(stale_minutes=10)

# Queue statistics
stats = await db.queue.get_queue_stats()

# Dead letter queue
dead_jobs = await db.queue.get_dead_letter_jobs(limit=100)
```

**Retry Logic:**
- Retryable failures return to `pending` with exponential `retry_at`/`ready_at`
  backoff and reduced priority.
- The third failed attempt moves the exact claimed version to `dead_letter`.
- Terminal failures move directly to `failed`; a newer desired generation clears
  the old claim and begins a fresh attempt cycle.

`PipelineLifecycleRepository.get_operational_snapshot()` is the durable status
read model. In addition to current queue/Batch/outbox state, it reports actionable
outbox age, unresolved queue-publication dead letters, tokenless/stale claims,
last-hour latency percentiles, a 24-hour attempt series grouped by job type/lane/
outcome, and a separate submitted/terminal Batch series. After migration 035,
newly activated Batch rows measure provider elapsed time from `submitted_at`;
the migration uses `created_at` only as the best available estimate for legacy
rows.

---

### 6. SearchRepository (259 lines)

**Methods:**

```python
# Full-text search (PostgreSQL FTS with ts_rank relevance)
meetings = await db.search.search_meetings_fulltext("affordable housing", banana="paloaltoCA")

# Topic-based search (normalized tables)
meetings = await db.search.search_meetings_by_topic("housing", banana="paloaltoCA")
matters = await db.search.search_matters_by_topic("housing", banana="paloaltoCA")
items = await db.search.search_items_by_topic("housing", banana="paloaltoCA")

# Popular topics
topics = await db.search.get_popular_topics(banana="paloaltoCA", limit=10)
```

**FTS Implementation:**
- Stored `search_vector` tsvector columns (generated, not computed at query time)
- GIN indexes on both stored and expression-based vectors
- `plainto_tsquery()` for user input, `ts_rank()` for relevance

---

### 7. UserlandRepository (794 lines)

**User Operations:**
```python
user = await db.userland.get_user(user_id)
user = await db.userland.get_user_by_email("user@example.com")
await db.userland.create_user(user_obj)
await db.userland.update_last_login(user_id)
count = await db.userland.get_user_count()
```

**Alert Operations:**
```python
await db.userland.create_alert(alert_obj)
alert = await db.userland.get_alert(alert_id)
alerts = await db.userland.get_alerts(user_id, active_only=True)
active = await db.userland.get_active_alerts()
await db.userland.update_alert(alert_id, cities=[...], criteria={...})
await db.userland.delete_alert(alert_id)
```

**Alert Match Operations:**
```python
await db.userland.create_match(match_obj)
matches = await db.userland.get_matches(alert_id, notified=False)
await db.userland.mark_notified(match_id)
counts = await db.userland.get_match_counts(alert_ids)
```

**Magic Link & Refresh Token Security:**
```python
# Magic links (single-use)
is_used = await db.userland.is_magic_link_used(token_hash)
await db.userland.mark_magic_link_used(token_hash)
await db.userland.cleanup_expired_magic_links()

# Refresh tokens (revocable)
await db.userland.create_refresh_token(user_id, token_hash, expires_at)
user_id = await db.userland.validate_refresh_token(token_hash)
await db.userland.revoke_refresh_token(token_hash)
await db.userland.revoke_all_user_tokens(user_id)
```

**City Demand Tracking:**
```python
await db.userland.record_city_request("berkeleyCA")
demanded = await db.userland.get_demanded_cities()
pending = await db.userland.get_pending_city_requests()
```

---

### 8. CouncilMemberRepository (761 lines)

**Methods:**

```python
# Find or create council member (normalizes name for matching)
member = await db.council_members.find_or_create_member(
    banana="nashvilleTN",
    name="Freddie O'Connell",
    appeared_at=datetime(2025, 11, 10)
)

# Update metadata
await db.council_members.update_member_metadata(member.id, title="Mayor", district="At-Large")

# Get members
members = await db.council_members.get_members_by_city("nashvilleTN", status="active")
member = await db.council_members.get_member_by_id(member_id)

# Sponsorships
await db.council_members.create_sponsorship(member.id, matter_id, is_primary=True)
await db.council_members.link_sponsors_to_matter("nashvilleTN", matter_id, ["Jane Doe", "John Smith"])
sponsors = await db.council_members.get_sponsors_for_matter(matter_id)
matters = await db.council_members.get_matters_by_sponsor(member.id)

# Votes
await db.council_members.record_vote(member.id, matter_id, meeting_id, "yes")
await db.council_members.record_votes_for_matter("nashvilleTN", matter_id, meeting_id, votes_list)
votes = await db.council_members.get_votes_for_meeting(meeting_id)
votes = await db.council_members.get_votes_for_matter(matter_id)
record = await db.council_members.get_member_voting_record(member.id, limit=100)
tally = await db.council_members.get_vote_tally_for_matter(matter_id)
```

**Name Normalization:**
- Strips whitespace, lowercases for matching
- Handles: "Freddie O'Connell" = "FREDDIE O'CONNELL" = " Freddie O'connell "
- ID format: `{banana}_cm_{16-char-sha256-hash}`

---

### 9. CommitteeRepository (598 lines)

**Methods:**

```python
# Find or create committee
committee = await db.committees.find_or_create_committee(
    banana="sanfranciscoCA",
    name="Planning Commission",
    description="Oversees land use"
)

# Get committees
committee = await db.committees.get_committee_by_id(committee_id)
committees = await db.committees.get_committees_by_city("sanfranciscoCA", status="active")

# Membership management
await db.committees.add_member_to_committee(committee.id, member.id, role="Chair")
await db.committees.remove_member_from_committee(committee.id, member.id)

# Roster queries (current and historical)
members = await db.committees.get_committee_members(committee.id, active_only=True)
members = await db.committees.get_committee_members(committee.id, as_of=datetime(2024, 6, 1))
counts = await db.committees.get_committee_member_counts(committee_ids)

# Member's committees
committees = await db.committees.get_member_committees(member.id, active_only=True)

# Vote tracking
await db.committees.update_matter_appearance_outcome(matter_id, meeting_id, item_id, vote_outcome="passed")
history = await db.committees.get_committee_vote_history(committee.id, limit=50)
```

**Historical Queries:**
- `joined_at` / `left_at` enable "who was on committee X when matter Y was voted?"
- `left_at = NULL` means currently serving

---

### 10. EngagementRepository (176 lines)

**Methods:**

```python
# Watch/unwatch entities
await db.engagement.watch(user_id, entity_type="matter", entity_id=matter_id)
await db.engagement.unwatch(user_id, "matter", matter_id)
watching = await db.engagement.is_watching(user_id, "matter", matter_id)
count = await db.engagement.get_watch_count("matter", matter_id)
watches = await db.engagement.get_user_watches(user_id, entity_type="matter")

# Activity logging
await db.engagement.log_activity(user_id, session_id, "view", "meeting", meeting_id)
```

**Entity Types:** `matter`, `meeting`, `topic`, `city`, `council_member`

---

### 11. FeedbackRepository (343 lines)

**Methods:**

```python
# Ratings (1-5 stars)
await db.feedback.submit_rating(user_id, session_id, "item", item_id, rating=4)
stats = await db.feedback.get_rating_stats("item", item_id)
ratings = await db.feedback.get_user_ratings(user_id)

# Issue reporting
await db.feedback.submit_issue(user_id, session_id, "item", item_id, "inaccurate", "Summary misses budget details")
issues = await db.feedback.get_issues(entity_type="item", status="open")
issue = await db.feedback.get_issue_by_id(issue_id)
await db.feedback.update_issue_status(issue_id, "resolved", admin_notes="Fixed")
await db.feedback.resolve_issue(issue_id, admin_notes="Fixed")
count = await db.feedback.get_issue_count("item", item_id)

# Quality scores (denormalized)
score = await db.feedback.get_quality_score("item", item_id)
await db.feedback.update_quality_score("item", item_id)
```

---

### 12. Helpers Module (185 lines)

**Shared utilities for consistent object construction across repositories.**

```python
from database.repositories_async.helpers import (
    # JSONB deserialization
    deserialize_attachments,       # JSONB -> List[AttachmentInfo]
    deserialize_metadata,          # JSONB -> MatterMetadata
    deserialize_participation,     # JSONB -> ParticipationInfo
    deserialize_city_participation,# JSONB -> CityParticipation
    deserialize_agenda_sources,    # JSONB -> list of dicts

    # Topic operations (eliminates N+1 queries)
    fetch_topics_for_ids,          # Batch fetch topics from normalized tables
    replace_entity_topics,         # DELETE + INSERT topic replacement
    replace_entity_topics_batch,   # Batch topic replacement

    # Object builders (DB row -> domain model)
    build_matter,
    build_meeting,
    build_agenda_item,
)
```

---

### 13. DeliberationRepository (757 lines)

**Methods:**

```python
# Create/get deliberation
delib = await db.deliberation.create_deliberation(matter_id, "nashvilleTN", topic="Housing Amendment")
delib = await db.deliberation.get_deliberation(deliberation_id)
delib = await db.deliberation.get_deliberation_for_matter(matter_id)
await db.deliberation.close_deliberation(deliberation_id)

# Pseudonymous participants
num = await db.deliberation.get_or_assign_participant_number(deliberation_id, user_id)

# Trust-based moderation
is_trusted = await db.deliberation.is_user_trusted(user_id)
await db.deliberation.mark_user_trusted(user_id)

# Comments
comment = await db.deliberation.create_comment(deliberation_id, user_id, participant_number, "I support this")
comments = await db.deliberation.get_comments(deliberation_id, include_rejected=False)
pending = await db.deliberation.get_pending_comments(deliberation_id)
await db.deliberation.moderate_comment(comment_id, approve=True)

# Votes (-1=disagree, 0=pass, 1=agree)
await db.deliberation.record_vote(deliberation_id, user_id, participant_number, position, vote_type, comment_id)
user_votes = await db.deliberation.get_user_votes(deliberation_id, user_id)

# Clustering
matrix = await db.deliberation.get_vote_matrix(deliberation_id)
await db.deliberation.save_results(deliberation_id, clustering_results)
results = await db.deliberation.get_results(deliberation_id)

# Stats
stats = await db.deliberation.get_deliberation_stats(deliberation_id)
```

---

### 14. HappeningRepository (125 lines)

**Methods:**

```python
items = await db.happening.get_happening_items(banana="nashvilleTN", limit=10)
all_items = await db.happening.get_all_active(limit=100)
deleted = await db.happening.clear_expired()
cities = await db.happening.get_cities_with_happening()
```

---

### 15. vote_utils.py (47 lines)

**Shared vote tally and outcome computation logic.**

```python
from database.vote_utils import compute_vote_tally, determine_vote_outcome, VOTE_MAP

# Normalize vote values
VOTE_MAP = {
    "yes": "yes", "aye": "yes", "yea": "yes",
    "no": "no", "nay": "no",
    "abstain": "abstain", "abstained": "abstain", "recused": "abstain",
    "absent": "absent", "excused": "absent", "not present": "absent",
    "present": "present", "not_voting": "present",
}

tally = compute_vote_tally(votes)     # {"yes": 2, "no": 1, "abstain": 1, "absent": 0, "present": 0}
outcome = determine_vote_outcome(tally)  # "passed" | "failed" | "tabled" | "no_vote"
```

---

## ID Generation (`id_generation.py`, 721 lines)

**Deterministic, collision-free ID generation. Single source of truth - no adapter generates final IDs.**

### ID Formats

| Entity | Format | Hash | Example |
|--------|--------|------|---------|
| Meeting | `{banana}_{8-char-md5}` | MD5 of `{banana}:{vendor_id}:{date}:{title}` | `chicagoIL_a3f2c1d4` |
| Item | `{meeting_id}_{suffix}` | Vendor ID or sequence-based | `chicagoIL_a3f2c1d4_ord2024-123` |
| Matter | `{banana}_{16-char-sha256}` | SHA256 of `{banana}:file:{matter_file}` | `nashvilleTN_7a8f3b2c1d9e4f5a` |
| Council Member | `{banana}_cm_{16-char-sha256}` | SHA256 of `{banana}:council_member:{normalized_name}` | `chicagoIL_cm_a1b2c3d4e5f6g7h8` |
| Committee | `{banana}_comm_{16-char-sha256}` | SHA256 of `{banana}:committee:{normalized_name}` | `chicagoIL_comm_a1b2c3d4e5f6g7h8` |

### Key Functions

```python
from database.id_generation import (
    # Meeting IDs
    generate_meeting_id(banana, vendor_id, date, title),    # date may be None -> stable undated ID
    validate_meeting_id(meeting_id),                         # -> bool
    hash_meeting_id(meeting_id),                            # -> 16-char hex (for URL slugs)

    # Item IDs
    generate_item_id(meeting_id, sequence, vendor_item_id),  # -> "chicagoIL_a3f2c1d4_ord2024-123"
    validate_item_id(item_id),
    extract_meeting_id_from_item_id(item_id),               # -> "chicagoIL_a3f2c1d4"

    # Matter IDs (fallback hierarchy: matter_file > matter_id > title)
    generate_matter_id(banana, matter_file, matter_id, title),
    validate_matter_id(matter_id),
    extract_banana_from_matter_id(matter_id),
    matter_ids_match(banana, file1, id1, file2, id2),

    # Council member IDs
    generate_council_member_id(banana, name),               # -> "chicagoIL_cm_..."
    validate_council_member_id(member_id),
    normalize_sponsor_name(name),                           # -> lowercase, trimmed

    # Committee IDs
    generate_committee_id(banana, name),                    # -> "chicagoIL_comm_..."
    validate_committee_id(committee_id),
    normalize_committee_name(name),

    # Title normalization (for cities without stable vendor IDs)
    normalize_title_for_matter_id(title),                   # -> normalized or None (generic)
)
```

**Matter ID Fallback Hierarchy:**
1. `matter_file` ALONE - Public legislative file number (ignores matter_id when present)
2. `matter_id` ALONE - Backend vendor identifier (only when no matter_file)
3. `title` - Normalized title (for cities without stable vendor IDs like Palo Alto PrimeGov)

---

## Database Facade (`db_postgres.py`, 419 lines)

The `Database` class provides convenience wrappers that coordinate across repositories:

```python
# Lifecycle
db = await Database.create(dsn=None, min_size=5, max_size=20)
await db.init_schema()  # Apply schema from SQL files
await db.close()

# City lookups (delegates to CityRepository)
city = await db.get_city(banana="paloaltoCA")
city = await db.get_city(zipcode="94301")
city = await db.get_city(name="Palo Alto", state="CA")
cities = await db.get_cities(state="CA", vendor="primegov", include_zipcodes=True)
names = await db.get_city_names()

# Meeting queries
meeting = await db.get_meeting(meeting_id)
meetings = await db.get_meetings(bananas=["paloaltoCA"], limit=50, exclude_cancelled=True)

# Agenda items with optional matter eager-loading
items = await db.get_agenda_items(meeting_id, load_matters=True)
items_map = await db.get_items_for_meetings(meeting_ids, load_matters=True)  # Eliminates N+1
has_summaries = await db.get_has_summarized_items(meeting_ids)  # Lightweight check

# Search
meetings = await db.search_meetings_by_topic("housing", city_banana="paloaltoCA")
topics = await db.get_popular_topics(limit=20)
items = await db.get_items_by_topic(meeting_id, "housing")

# Matters
matter = await db.get_matter(matter_id)
matters = await db.get_matters_batch(matter_ids)
meeting = await db.get_random_meeting_with_items()

# Stats and metrics
stats = await db.get_stats()              # active_cities, total_meetings, summary_rate
metrics = await db.get_platform_metrics()  # Comprehensive counts + vote breakdown by city
queue_stats = await db.get_queue_stats()
city_stats = await db.get_city_meeting_stats(["paloaltoCA", "oaklandCA"])

# Census data
states = await db.get_states_for_city_name("Portland")  # ["OR", "ME", "TX"]
```

---

## Configuration

**Required Environment Variables:**
```bash
# PostgreSQL connection (defaults to config.get_postgres_dsn())
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=engagic
POSTGRES_USER=engagic
POSTGRES_PASSWORD=***

# Connection pool settings
POSTGRES_POOL_MIN_SIZE=5   # Minimum connections
POSTGRES_POOL_MAX_SIZE=20  # Maximum connections
```

**Connection Pool:**
- **asyncpg pool:** 5-20 connections shared across all repositories
- **Automatic JSONB codec:** Python dicts serialized to/from PostgreSQL JSONB (with Pydantic model_dump support)
- **Command timeout:** 60 seconds
- **Pool lifecycle:** Created at `Database.create()`, closed at `db.close()`

---

## Key Patterns

### Preservation on Re-Sync (UPSERT)

Re-syncing cities should NOT overwrite existing summaries:

```sql
INSERT INTO meetings (...) VALUES (...)
ON CONFLICT(id) DO UPDATE SET
    summary = CASE
        WHEN excluded.summary IS NOT NULL THEN excluded.summary
        ELSE meetings.summary
    END
```

### Normalized Topics

Topics stored in separate tables (`meeting_topics`, `item_topics`, `matter_topics`) instead of JSON arrays:
- Efficient filtering: `WHERE topic = 'housing'` uses B-tree index
- No JSON array scanning
- Trade-off: More joins, but PostgreSQL handles efficiently with proper indexes

### Stored Search Vectors

```sql
ALTER TABLE meetings ADD COLUMN search_vector tsvector
    GENERATED ALWAYS AS (to_tsvector('english', COALESCE(title, '') || ' ' || COALESCE(summary, ''))) STORED;
```

Generated columns auto-update when title/summary change. GIN indexes on these stored columns are 5-10x faster than expression-based FTS indexes.

### Atomic Queue Dequeue

The repository selects one ready row with `FOR UPDATE OF q SKIP LOCKED`, ordered
by priority, `last_enqueued_at`, and ID. In the same transaction it changes the
row to `processing`, assigns a fresh UUID `claim_token`, and sets `claimed_at`,
`heartbeat_at`, and `started_at`. Heartbeats and final transitions require
`id + claim_token + work_version`, so an expired worker cannot settle a replacement
claim.

---

## Migration System (`migrate.py`, 271 lines)

```bash
uv run python -m database.migrate              # Apply pending migrations
uv run python -m database.migrate --status     # Show migration status
uv run python -m database.migrate --rollback 1 # Rollback last migration
```

- Migrations: numbered SQL files in `database/migrations/` (e.g., `001_name.sql`)
- Each runs in a transaction (all-or-nothing)
- Applied migrations tracked in `schema_migrations` table
- Optional rollback via `001_name.down.sql` files

At the 2026-08-07 rollout handoff, production had applied migrations 029-036 in
quiescent migration windows. Migration 035 adds the Batch provider-acceptance
clock (`submitted_at`); migration 036 permits an authoritative appearance date
to remain NULL when its meeting is undated. Check live status before acting
rather than treating this point-in-time note as a substitute for the migration
ledger.

---

## Related Modules

- **`pipeline/`** - Orchestration and processing logic
- **`vendors/`** - Adapter implementations that populate database
- **`analysis/`** - LLM analysis that creates summaries
- **`server/`** - API that reads from database
