-- Engagic PostgreSQL Database Schema
-- Migrated from SQLite: 2025-11-22
--
-- Key improvements over SQLite:
-- - Normalized JSON columns (topics) to relational tables for indexing
-- - JSONB for complex structures (attachments, metadata, participation)
-- - Full-text search via tsvector and GIN indexes
-- - Connection pooling support (asyncpg)
-- - Proper SERIAL for auto-increment
-- - Cross-jurisdiction collision prevention via composite keys with banana
-- - PostGIS for jurisdiction boundary polygons (map visualization)

-- =======================
-- EXTENSIONS
-- =======================

CREATE EXTENSION IF NOT EXISTS postgis;

-- Jurisdictions table: Core entity registry (cities, counties, utility boards, transit authorities, etc.)
CREATE TABLE IF NOT EXISTS jurisdictions (
    banana TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    state TEXT NOT NULL,
    vendor TEXT NOT NULL,
    slug TEXT NOT NULL,
    extra_vendors JSONB,  -- Additive [{vendor, slug}] for rare multi-vendor jurisdictions (e.g. Maricopa: BoS on OnBase, commissions on CivicPlus)
    type TEXT NOT NULL DEFAULT 'city',  -- city, county, school_district, utility, transit, water_district, etc.
    county_banana TEXT,  -- FK to parent county jurisdiction (NULL for counties and non-county entities)
    status TEXT DEFAULT 'active',
    participation JSONB,  -- Jurisdiction-level participation config: {testimony_url, testimony_email, process_url}
    population INTEGER,  -- Population from Census data
    geom geometry(MultiPolygon, 4326),  -- Boundary from Census TIGER/Line (WGS84)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (county_banana) REFERENCES jurisdictions(banana) ON DELETE SET NULL,
    UNIQUE(name, state)
);

-- City zipcodes: Many-to-many relationship
CREATE TABLE IF NOT EXISTS zipcodes (
    banana TEXT NOT NULL,
    zipcode TEXT NOT NULL,
    is_primary BOOLEAN DEFAULT FALSE,
    FOREIGN KEY (banana) REFERENCES jurisdictions(banana) ON DELETE CASCADE,
    PRIMARY KEY (banana, zipcode)
);

-- Meetings table: Meeting data with optional summaries
-- NOTE: topics and participation moved to separate tables for PostgreSQL normalization
CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    banana TEXT NOT NULL,
    title TEXT NOT NULL,
    date TIMESTAMP,
    agenda_url TEXT,
    agenda_sources JSONB,  -- [{type, url, label}] for multi-agenda provenance (PrimeGov)
    packet_url TEXT,
    minutes_url TEXT,  -- Minutes doc/page; published post-meeting, fills in on resync (migration 026)
    summary TEXT,
    participation JSONB,  -- Complex structure: {email, phone, zoom}, keep as JSONB
    status TEXT,
    processing_status TEXT DEFAULT 'pending',
    processing_method TEXT,
    processing_time REAL,
    committee_id TEXT,  -- FK to committees (a meeting is an occurrence of a committee)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (banana) REFERENCES jurisdictions(banana) ON DELETE CASCADE,
    FOREIGN KEY (committee_id) REFERENCES committees(id) ON DELETE SET NULL
);

-- Meeting topics: Normalized from meetings.topics (was JSON array)
-- Enables efficient topic filtering and indexing
CREATE TABLE IF NOT EXISTS meeting_topics (
    meeting_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE,
    PRIMARY KEY (meeting_id, topic)
);

-- City Matters: Canonical representation of legislative items
-- Matters-First Architecture: Each matter has ONE canonical summary
-- that is reused across all appearances (deduplication)
-- CRITICAL: id includes city_banana in hash to prevent cross-city collisions
CREATE TABLE IF NOT EXISTS city_matters (
    id TEXT PRIMARY KEY,  -- Composite hash: includes city_banana to prevent cross-city collisions
    banana TEXT NOT NULL,  -- Scopes matter to specific city
    matter_id TEXT,        -- Vendor-specific UUID (may be unstable)
    matter_file TEXT,      -- Stable public file number (e.g., "BL2025-1098")
    matter_type TEXT,
    title TEXT NOT NULL,
    sponsors JSONB,        -- Array of sponsor names, keep as JSONB
    canonical_summary TEXT,
    canonical_topics JSONB,  -- Will normalize to matter_topics table
    attachments JSONB,     -- Complex structure: [{url, title, pages}], keep as JSONB
    metadata JSONB,        -- Flexible field for vendor-specific data
    first_seen TIMESTAMP,
    last_seen TIMESTAMP,
    appearance_count INTEGER DEFAULT 1,
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'passed', 'failed', 'tabled', 'withdrawn', 'referred', 'amended', 'vetoed', 'enacted')),
    final_vote_date TIMESTAMP,  -- Date when matter reached terminal disposition
    quality_score REAL,    -- Denormalized from ratings for efficient queries
    rating_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (banana) REFERENCES jurisdictions(banana) ON DELETE CASCADE
);

-- Matter topics: Normalized from city_matters.canonical_topics
CREATE TABLE IF NOT EXISTS matter_topics (
    matter_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    FOREIGN KEY (matter_id) REFERENCES city_matters(id) ON DELETE CASCADE,
    PRIMARY KEY (matter_id, topic)
);

-- Agenda items: Individual items within meetings
-- matter_id stores COMPOSITE HASHED ID matching city_matters.id
-- CRITICAL: matter_id references city_matters.id which includes city_banana
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    title TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    attachments JSONB,     -- Complex structure: [{url, title, pages}], keep as JSONB
    attachment_hash TEXT,
    body_text TEXT,        -- Coversheet/detail page text (when no PDF attachments)
    matter_id TEXT,        -- References city_matters.id (includes city_banana in hash)
    matter_file TEXT,      -- Denormalized for query performance
    matter_type TEXT,
    agenda_number TEXT,
    sponsors JSONB,        -- Array of sponsor names
    summary TEXT,
    prompts_version TEXT,  -- Prompts-file version that produced the summary (NULL = pre-2026-06; backfill target)
    topics JSONB,          -- Will normalize to item_topics table
    quality_score REAL,    -- Denormalized from ratings for efficient queries
    rating_count INTEGER DEFAULT 0,
    filter_reason TEXT,    -- Why item was skipped: 'procedural', 'ceremonial', 'administrative', or NULL
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE,
    FOREIGN KEY (matter_id) REFERENCES city_matters(id) ON DELETE SET NULL
);

-- Item topics: Normalized from items.topics
CREATE TABLE IF NOT EXISTS item_topics (
    item_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE,
    PRIMARY KEY (item_id, topic)
);

-- Matter Appearances: Timeline tracking for matters across meetings
-- Junction table linking matters to meetings via agenda items
CREATE TABLE IF NOT EXISTS matter_appearances (
    id BIGSERIAL PRIMARY KEY,  -- PostgreSQL auto-increment
    matter_id TEXT NOT NULL,
    meeting_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    appeared_at TIMESTAMP NOT NULL,
    committee TEXT,
    action TEXT,
    vote_outcome TEXT CHECK (vote_outcome IS NULL OR vote_outcome IN ('passed', 'failed', 'tabled', 'withdrawn', 'referred', 'amended', 'unknown', 'no_vote')),
    vote_tally JSONB,  -- {yes: N, no: N, abstain: N, absent: N}
    committee_id TEXT,  -- FK to committees for relational queries
    sequence INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (matter_id) REFERENCES city_matters(id) ON DELETE CASCADE,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE,
    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE,
    FOREIGN KEY (committee_id) REFERENCES committees(id) ON DELETE SET NULL,
    UNIQUE(matter_id, meeting_id, item_id)
);

-- Processing cache: Track PDF processing for cost optimization
CREATE TABLE IF NOT EXISTS cache (
    packet_url TEXT PRIMARY KEY,
    content_hash TEXT,
    processing_method TEXT,
    processing_time REAL,
    cache_hit_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Processing queue: Decoupled processing queue (agenda-first, item-level)
CREATE TABLE IF NOT EXISTS queue (
    id BIGSERIAL PRIMARY KEY,  -- PostgreSQL auto-increment
    source_url TEXT NOT NULL UNIQUE,
    meeting_id TEXT,
    banana TEXT,
    job_type TEXT,
    payload JSONB,  -- Was TEXT in SQLite, JSONB is native in PostgreSQL
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'dead_letter')),
    priority INTEGER DEFAULT 0,
    retry_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    failed_at TIMESTAMP,
    error_message TEXT,
    processing_metadata JSONB,  -- Was TEXT, now JSONB
    FOREIGN KEY (banana) REFERENCES jurisdictions(banana) ON DELETE CASCADE,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

-- Batch jobs: durable record of submitted Gemini Batch API jobs (see
-- migration 024). The submit path writes a row and releases its lane slot; a
-- collector polls open rows and ingests results on Gemini's terminal verdict.
-- Running jobs are never cancelled. One row per submitted chunk.
CREATE TABLE IF NOT EXISTS batch_jobs (
    id BIGSERIAL PRIMARY KEY,
    gemini_job_name TEXT NOT NULL UNIQUE,
    meeting_id TEXT NOT NULL,
    banana TEXT,
    chunk_num INTEGER NOT NULL DEFAULT 1,
    item_ids JSONB NOT NULL,
    cache_name TEXT,
    prompts_version TEXT,
    meeting_meta JSONB,
    status TEXT NOT NULL DEFAULT 'submitted'
        CHECK (status IN ('submitted', 'collected', 'failed')),
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_polled_at TIMESTAMP,
    collected_at TIMESTAMP,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE,
    FOREIGN KEY (banana) REFERENCES jurisdictions(banana) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_batch_jobs_open
    ON batch_jobs (created_at)
    WHERE status = 'submitted';
CREATE INDEX IF NOT EXISTS idx_batch_jobs_meeting
    ON batch_jobs (meeting_id);

-- Ground-truth corpus index (see migration 025 / docs/CORPUS_ARCHITECTURE.md).
-- Original bytes and extracted text live in R2 (engagic-corpus bucket),
-- content-addressed by sha256(source bytes); these rows are the pointer +
-- provenance layer. No FKs into meetings/items: the corpus is an append-only
-- archive that outlives any particular meeting row.
CREATE TABLE IF NOT EXISTS document_blob (
    content_sha256 TEXT PRIMARY KEY,
    bytes BIGINT NOT NULL,
    content_type TEXT,
    original_key TEXT,              -- R2 key of archived source bytes; NULL until archived
    text_key TEXT,                  -- R2 key of extracted text; NULL until extracted
    extract_method TEXT,            -- 'pymupdf' | 'pymupdf+ocr' | 'antiword' | ...
    extract_version TEXT,           -- bump corpus.store.EXTRACT_VERSION to force re-extraction
    page_count INTEGER,
    ocr_page_count INTEGER,
    text_chars BIGINT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    text_extracted_at TIMESTAMP
);

-- Where these bytes have been seen: source_identity is the signature-stripped
-- attachment URL (pipeline.utils.attachment_identity), stable across re-signing.
CREATE TABLE IF NOT EXISTS document_source (
    content_sha256 TEXT NOT NULL REFERENCES document_blob(content_sha256) ON DELETE CASCADE,
    source_identity TEXT NOT NULL,
    banana TEXT,
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (content_sha256, source_identity)
);
CREATE INDEX IF NOT EXISTS idx_document_source_identity
    ON document_source (source_identity, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_document_blob_untexted
    ON document_blob (created_at)
    WHERE text_key IS NULL;

-- Tenants table: B2B customers (Phase 5)
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    api_key TEXT UNIQUE NOT NULL,
    webhook_url TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tenant coverage: Which cities each tenant tracks
CREATE TABLE IF NOT EXISTS tenant_coverage (
    tenant_id TEXT NOT NULL,
    banana TEXT NOT NULL,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    FOREIGN KEY (banana) REFERENCES jurisdictions(banana) ON DELETE CASCADE,
    PRIMARY KEY (tenant_id, banana)
);

-- Tenant keywords: Topics tenants care about
CREATE TABLE IF NOT EXISTS tenant_keywords (
    tenant_id TEXT NOT NULL,
    keyword TEXT NOT NULL,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    PRIMARY KEY (tenant_id, keyword)
);

-- Tracked items: Ordinances, proposals, etc. (Phase 6)
CREATE TABLE IF NOT EXISTS tracked_items (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    item_type TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    banana TEXT NOT NULL,
    first_mentioned_meeting_id TEXT,
    first_seen TIMESTAMP,
    last_seen TIMESTAMP,
    status TEXT DEFAULT 'active',
    metadata JSONB,  -- Was TEXT, now JSONB
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE,
    FOREIGN KEY (banana) REFERENCES jurisdictions(banana) ON DELETE CASCADE,
    FOREIGN KEY (first_mentioned_meeting_id) REFERENCES meetings(id) ON DELETE SET NULL
);

-- Tracked item meetings: Link tracked items to meetings
CREATE TABLE IF NOT EXISTS tracked_item_meetings (
    tracked_item_id TEXT NOT NULL,
    meeting_id TEXT NOT NULL,
    mentioned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    excerpt TEXT,
    FOREIGN KEY (tracked_item_id) REFERENCES tracked_items(id) ON DELETE CASCADE,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE,
    PRIMARY KEY (tracked_item_id, meeting_id)
);

-- =======================
-- COUNCIL MEMBERS & VOTING (Phase 2)
-- =======================
-- Normalizes sponsor data from city_matters.sponsors JSONB array
-- into proper relational tables for tracking elected officials.

-- Council Members: Elected officials registry per city
CREATE TABLE IF NOT EXISTS council_members (
    id TEXT PRIMARY KEY,  -- Hash of (banana + normalized_name) for cross-city safety
    banana TEXT NOT NULL,
    name TEXT NOT NULL,  -- Display name as extracted from vendor
    normalized_name TEXT NOT NULL,  -- Lowercase, trimmed for matching
    title TEXT,  -- Role: "Council Member", "Mayor", "Alderman", etc.
    district TEXT,  -- Ward/district number if applicable
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'former', 'unknown')),
    first_seen TIMESTAMP,  -- First appearance (sponsor or vote)
    last_seen TIMESTAMP,  -- Most recent activity
    sponsorship_count INTEGER DEFAULT 0,  -- Denormalized for quick stats
    vote_count INTEGER DEFAULT 0,  -- Denormalized count of votes cast
    metadata JSONB,  -- Vendor-specific fields (photo_url, party, etc.)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (banana) REFERENCES jurisdictions(banana) ON DELETE CASCADE,
    UNIQUE(banana, normalized_name)
);

-- Sponsorships: Links council members to matters they sponsor
CREATE TABLE IF NOT EXISTS sponsorships (
    id BIGSERIAL PRIMARY KEY,
    council_member_id TEXT NOT NULL,
    matter_id TEXT NOT NULL,
    is_primary BOOLEAN DEFAULT FALSE,  -- Primary sponsor vs co-sponsor
    sponsor_order INTEGER,  -- Order in sponsor list (1 = first listed)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (council_member_id) REFERENCES council_members(id) ON DELETE CASCADE,
    FOREIGN KEY (matter_id) REFERENCES city_matters(id) ON DELETE CASCADE,
    UNIQUE(council_member_id, matter_id)
);

-- Votes: Individual voting records per member per matter per meeting
CREATE TABLE IF NOT EXISTS votes (
    id BIGSERIAL PRIMARY KEY,
    council_member_id TEXT NOT NULL,
    matter_id TEXT NOT NULL,
    meeting_id TEXT NOT NULL,  -- Critical: same matter can be voted multiple times
    vote TEXT NOT NULL CHECK (vote IN ('yes', 'no', 'abstain', 'absent', 'present', 'recused', 'not_voting')),
    vote_date TIMESTAMP,  -- Date of vote (usually meeting date)
    sequence INTEGER,  -- Order in roll call if available
    metadata JSONB,  -- Vendor-specific (motion_id, voice_vote, etc.)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (council_member_id) REFERENCES council_members(id) ON DELETE CASCADE,
    FOREIGN KEY (matter_id) REFERENCES city_matters(id) ON DELETE CASCADE,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE,
    UNIQUE(council_member_id, matter_id, meeting_id)
);

-- =======================
-- COMMITTEES (Phase 2)
-- =======================
-- Registry of legislative bodies per city

CREATE TABLE IF NOT EXISTS committees (
    id TEXT PRIMARY KEY,  -- {banana}_comm_{16-char-hash}
    banana TEXT NOT NULL,
    name TEXT NOT NULL,  -- "Planning Commission", "Budget Committee", "City Council"
    normalized_name TEXT NOT NULL,  -- Lowercase for matching
    description TEXT,
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'unknown')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (banana) REFERENCES jurisdictions(banana) ON DELETE CASCADE,
    UNIQUE(banana, normalized_name)
);

-- Committee Members: Tracks which council members serve on which committees
-- Historical tracking via joined_at/left_at enables time-aware queries
CREATE TABLE IF NOT EXISTS committee_members (
    id BIGSERIAL PRIMARY KEY,
    committee_id TEXT NOT NULL,
    council_member_id TEXT NOT NULL,
    role TEXT,  -- "Chair", "Vice-Chair", "Member"
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    left_at TIMESTAMP,  -- NULL = currently serving
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (committee_id) REFERENCES committees(id) ON DELETE CASCADE,
    FOREIGN KEY (council_member_id) REFERENCES council_members(id) ON DELETE CASCADE,
    UNIQUE(committee_id, council_member_id, joined_at)
);

-- =======================
-- DELIBERATION (Phase 3)
-- =======================
-- Opinion clustering for civic engagement
-- Requires userland schema (schema_userland.sql) for user references

-- Deliberation sessions linked to matters
CREATE TABLE IF NOT EXISTS deliberations (
    id TEXT PRIMARY KEY,                    -- "delib_{matter_id}_{short_hash}"
    matter_id TEXT NOT NULL REFERENCES city_matters(id) ON DELETE CASCADE,
    banana TEXT NOT NULL,
    topic TEXT,                             -- Optional override of matter title
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    closed_at TIMESTAMPTZ
);

-- Track participant numbers per deliberation (for pseudonyms)
CREATE TABLE IF NOT EXISTS deliberation_participants (
    deliberation_id TEXT NOT NULL REFERENCES deliberations(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES userland.users(id) ON DELETE CASCADE,
    participant_number INTEGER NOT NULL,
    joined_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (deliberation_id, user_id)
);

-- User-submitted comments
CREATE TABLE IF NOT EXISTS deliberation_comments (
    id SERIAL PRIMARY KEY,
    deliberation_id TEXT NOT NULL REFERENCES deliberations(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES userland.users(id) ON DELETE CASCADE,
    participant_number INTEGER NOT NULL,    -- Pseudonym: "Participant 1", "Participant 2"
    txt TEXT NOT NULL,
    mod_status INTEGER DEFAULT 0,           -- 0=pending, 1=approved, -1=hidden
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Votes on comments
CREATE TABLE IF NOT EXISTS deliberation_votes (
    comment_id INTEGER NOT NULL REFERENCES deliberation_comments(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES userland.users(id) ON DELETE CASCADE,
    vote SMALLINT NOT NULL CHECK (vote IN (-1, 0, 1)),  -- -1=disagree, 0=pass, 1=agree
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (comment_id, user_id)
);

-- Cached clustering results
CREATE TABLE IF NOT EXISTS deliberation_results (
    deliberation_id TEXT PRIMARY KEY REFERENCES deliberations(id) ON DELETE CASCADE,
    n_participants INTEGER NOT NULL,
    n_comments INTEGER NOT NULL,
    k INTEGER NOT NULL,                     -- Number of clusters
    positions JSONB,                        -- [[x,y], ...] per participant
    clusters JSONB,                         -- {user_id: cluster_id}
    cluster_centers JSONB,                  -- [[x,y], ...] per cluster
    consensus JSONB,                        -- {comment_id: score}
    group_votes JSONB,                      -- Per-cluster vote tallies
    computed_at TIMESTAMPTZ DEFAULT NOW()
);

-- =======================
-- ANALYTICS & EVENTS
-- =======================

-- Happening Items: AI-curated important upcoming agenda items
-- Populated by autonomous analysis, surfaced in "Happening This Week" section
CREATE TABLE IF NOT EXISTS happening_items (
    id SERIAL PRIMARY KEY,
    banana TEXT NOT NULL REFERENCES jurisdictions(banana) ON DELETE CASCADE,
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    meeting_date TIMESTAMP NOT NULL,
    rank INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    expires_at TIMESTAMP NOT NULL,
    UNIQUE(banana, item_id)
);

-- Session Events: Anonymous user journey tracking
-- Events linked to IP hash (same as rate limiting), 7-day retention
CREATE TABLE IF NOT EXISTS session_events (
    id SERIAL PRIMARY KEY,
    ip_hash TEXT NOT NULL,
    event TEXT NOT NULL,
    url TEXT,
    properties JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Cleanup function for session events (7-day retention)
CREATE OR REPLACE FUNCTION cleanup_old_session_events()
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM session_events WHERE created_at < NOW() - INTERVAL '7 days';
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;

-- =======================
-- FULL-TEXT SEARCH OPTIMIZATION
-- =======================
-- Stored generated columns for faster FTS queries (5-10x improvement)

ALTER TABLE meetings ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (to_tsvector('english', COALESCE(title, '') || ' ' || COALESCE(summary, ''))) STORED;

ALTER TABLE items ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (to_tsvector('english', COALESCE(title, '') || ' ' || COALESCE(summary, ''))) STORED;

ALTER TABLE city_matters ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (to_tsvector('english', COALESCE(title, '') || ' ' || COALESCE(canonical_summary, ''))) STORED;

-- =======================
-- PERFORMANCE INDICES
-- =======================

-- Jurisdictions
CREATE INDEX IF NOT EXISTS idx_jurisdictions_type ON jurisdictions(type);
CREATE INDEX IF NOT EXISTS idx_jurisdictions_vendor ON jurisdictions(vendor);
CREATE INDEX IF NOT EXISTS idx_jurisdictions_state ON jurisdictions(state);
CREATE INDEX IF NOT EXISTS idx_jurisdictions_status ON jurisdictions(status);
CREATE INDEX IF NOT EXISTS idx_jurisdictions_population ON jurisdictions (population DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_jurisdictions_geom ON jurisdictions USING GIST (geom);  -- Spatial index for map queries

-- Zipcodes
CREATE INDEX IF NOT EXISTS idx_zipcodes_zipcode ON zipcodes(zipcode);

-- Meetings
CREATE INDEX IF NOT EXISTS idx_meetings_banana ON meetings(banana);
CREATE INDEX IF NOT EXISTS idx_meetings_date ON meetings(date);
CREATE INDEX IF NOT EXISTS idx_meetings_status ON meetings(processing_status);
CREATE INDEX IF NOT EXISTS idx_meetings_banana_date_nl ON meetings(banana, date DESC NULLS LAST);  -- City timeline: every consumer orders `date DESC NULLS LAST` (migration 021); also serves banana+date-range scans, so no plain (banana, date) twin needed
CREATE INDEX IF NOT EXISTS idx_meetings_committee ON meetings(committee_id);  -- For committee -> meetings queries

-- Meeting topics (new normalized table)
CREATE INDEX IF NOT EXISTS idx_meeting_topics_topic ON meeting_topics(topic);
CREATE INDEX IF NOT EXISTS idx_meeting_topics_meeting ON meeting_topics(meeting_id);

-- Items
CREATE INDEX IF NOT EXISTS idx_items_matter_file ON items(matter_file) WHERE matter_file IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_items_matter_id ON items(matter_id) WHERE matter_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_items_meeting_id ON items(meeting_id);
CREATE INDEX IF NOT EXISTS idx_items_matter_meeting ON items(matter_id, meeting_id) WHERE matter_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_items_meeting_summarized ON items(meeting_id) WHERE summary IS NOT NULL;  -- For stats query: meetings with summarized items
CREATE INDEX IF NOT EXISTS idx_items_filter_reason ON items(filter_reason) WHERE filter_reason IS NOT NULL;

-- Item topics (new normalized table)
CREATE INDEX IF NOT EXISTS idx_item_topics_topic ON item_topics(topic);
CREATE INDEX IF NOT EXISTS idx_item_topics_item ON item_topics(item_id);

-- City Matters
CREATE INDEX IF NOT EXISTS idx_city_matters_banana ON city_matters(banana);
CREATE INDEX IF NOT EXISTS idx_city_matters_matter_file ON city_matters(matter_file);
CREATE INDEX IF NOT EXISTS idx_city_matters_first_seen ON city_matters(first_seen);
CREATE INDEX IF NOT EXISTS idx_city_matters_status ON city_matters(status);
CREATE INDEX IF NOT EXISTS idx_city_matters_banana_file ON city_matters(banana, matter_file) WHERE matter_file IS NOT NULL;  -- Composite for matter lookup
CREATE INDEX IF NOT EXISTS idx_city_matters_final_vote ON city_matters(final_vote_date) WHERE final_vote_date IS NOT NULL;

-- Matter topics (new normalized table)
CREATE INDEX IF NOT EXISTS idx_matter_topics_topic ON matter_topics(topic);
CREATE INDEX IF NOT EXISTS idx_matter_topics_matter ON matter_topics(matter_id);

-- Matter Appearances
CREATE INDEX IF NOT EXISTS idx_matter_appearances_matter ON matter_appearances(matter_id);
CREATE INDEX IF NOT EXISTS idx_matter_appearances_meeting ON matter_appearances(meeting_id);
CREATE INDEX IF NOT EXISTS idx_matter_appearances_item ON matter_appearances(item_id);
CREATE INDEX IF NOT EXISTS idx_matter_appearances_date ON matter_appearances(appeared_at);
CREATE INDEX IF NOT EXISTS idx_matter_appearances_outcome ON matter_appearances(vote_outcome) WHERE vote_outcome IS NOT NULL;

-- Cache
CREATE INDEX IF NOT EXISTS idx_cache_hash ON cache(content_hash);

-- Queue
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status);
CREATE INDEX IF NOT EXISTS idx_queue_city ON queue(banana);
-- Composite index for get_next_for_processing() query (covers status, priority, created_at)
CREATE INDEX IF NOT EXISTS idx_queue_processing ON queue(status, priority DESC, created_at ASC);

-- Tenant tables
CREATE INDEX IF NOT EXISTS idx_tenant_coverage_city ON tenant_coverage(banana);
CREATE INDEX IF NOT EXISTS idx_tracked_items_tenant ON tracked_items(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tracked_items_city ON tracked_items(banana);
CREATE INDEX IF NOT EXISTS idx_tracked_items_status ON tracked_items(status);

-- Council members
CREATE INDEX IF NOT EXISTS idx_council_members_banana ON council_members(banana);
CREATE INDEX IF NOT EXISTS idx_council_members_normalized ON council_members(normalized_name);
CREATE INDEX IF NOT EXISTS idx_council_members_status ON council_members(status);
CREATE INDEX IF NOT EXISTS idx_council_members_banana_status ON council_members(banana, status);

-- Sponsorships
CREATE INDEX IF NOT EXISTS idx_sponsorships_member ON sponsorships(council_member_id);
CREATE INDEX IF NOT EXISTS idx_sponsorships_matter ON sponsorships(matter_id);
CREATE INDEX IF NOT EXISTS idx_sponsorships_primary ON sponsorships(is_primary) WHERE is_primary = TRUE;

-- Votes
CREATE INDEX IF NOT EXISTS idx_votes_member ON votes(council_member_id);
CREATE INDEX IF NOT EXISTS idx_votes_matter ON votes(matter_id);
CREATE INDEX IF NOT EXISTS idx_votes_meeting ON votes(meeting_id);
CREATE INDEX IF NOT EXISTS idx_votes_member_date ON votes(council_member_id, vote_date DESC);
CREATE INDEX IF NOT EXISTS idx_votes_value ON votes(vote);

-- Committees
CREATE INDEX IF NOT EXISTS idx_committees_banana ON committees(banana);
CREATE INDEX IF NOT EXISTS idx_committees_name ON committees(normalized_name);
CREATE INDEX IF NOT EXISTS idx_committees_status ON committees(status);

-- Committee members
CREATE INDEX IF NOT EXISTS idx_committee_members_committee ON committee_members(committee_id);
CREATE INDEX IF NOT EXISTS idx_committee_members_member ON committee_members(council_member_id);
CREATE INDEX IF NOT EXISTS idx_committee_members_active ON committee_members(committee_id) WHERE left_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_committee_members_dates ON committee_members(joined_at, left_at);

-- Matter appearances (committee_id)
CREATE INDEX IF NOT EXISTS idx_matter_appearances_committee_id ON matter_appearances(committee_id);

-- Deliberations
CREATE INDEX IF NOT EXISTS idx_delib_matter ON deliberations(matter_id);
CREATE INDEX IF NOT EXISTS idx_delib_banana ON deliberations(banana);
CREATE INDEX IF NOT EXISTS idx_delib_comments_delib ON deliberation_comments(deliberation_id);
CREATE INDEX IF NOT EXISTS idx_delib_comments_mod ON deliberation_comments(deliberation_id, mod_status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_delib_comments_unique ON deliberation_comments(deliberation_id, user_id, txt);
CREATE INDEX IF NOT EXISTS idx_delib_votes_comment ON deliberation_votes(comment_id);

-- Happening items
CREATE INDEX IF NOT EXISTS idx_happening_banana_expires ON happening_items(banana, expires_at);
CREATE INDEX IF NOT EXISTS idx_happening_expires ON happening_items(expires_at);

-- Session events
CREATE INDEX IF NOT EXISTS idx_session_events_ip_hash ON session_events(ip_hash, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_session_events_created ON session_events(created_at DESC);

-- =======================
-- FULL-TEXT SEARCH (PostgreSQL GIN indexes)
-- =======================

-- FTS indexes on stored search_vector columns (faster than expression indexes)
CREATE INDEX IF NOT EXISTS idx_meetings_search_vector ON meetings USING gin(search_vector);
CREATE INDEX IF NOT EXISTS idx_items_search_vector ON items USING gin(search_vector);
CREATE INDEX IF NOT EXISTS idx_city_matters_search_vector ON city_matters USING gin(search_vector);

-- Legacy expression-based FTS indexes (kept for backward compatibility)
CREATE INDEX IF NOT EXISTS idx_meetings_fts ON meetings
USING gin(to_tsvector('english', COALESCE(title, '') || ' ' || COALESCE(summary, '')));
CREATE INDEX IF NOT EXISTS idx_items_fts ON items
USING gin(to_tsvector('english', COALESCE(title, '') || ' ' || COALESCE(summary, '')));
CREATE INDEX IF NOT EXISTS idx_city_matters_fts ON city_matters
USING gin(to_tsvector('english', COALESCE(title, '') || ' ' || COALESCE(canonical_summary, '')));

-- Full-text search on council_members (name)
CREATE INDEX IF NOT EXISTS idx_council_members_fts ON council_members
USING gin(to_tsvector('english', name));

-- Full-text search on committees (name)
CREATE INDEX IF NOT EXISTS idx_committees_fts ON committees
USING gin(to_tsvector('english', name));

-- =======================
-- COVERING INDEXES (for common query patterns)
-- =======================

-- Meetings by city sorted by date (covering index for faster reads)
CREATE INDEX IF NOT EXISTS idx_meetings_banana_date_covering
    ON meetings(banana, date DESC) INCLUDE (id, title, summary);

-- Items by meeting sorted by sequence
CREATE INDEX IF NOT EXISTS idx_items_meeting_sequence
    ON items(meeting_id, sequence ASC);

-- Matters by city for city matters listing
CREATE INDEX IF NOT EXISTS idx_city_matters_banana_last_seen
    ON city_matters(banana, last_seen DESC);

-- =======================
-- COMMENTS FOR CRITICAL CONSTRAINTS
-- =======================

COMMENT ON TABLE city_matters IS 'Matters-First Architecture: Each legislative matter has ONE canonical summary reused across all appearances. The id field includes city_banana in hash to prevent cross-city collisions (e.g., BL2025-1098 can exist in multiple cities).';

COMMENT ON COLUMN city_matters.id IS 'Composite hash including city_banana to prevent cross-city collisions. Generated via fallback hierarchy: matter_file (preferred) → matter_id (vendor UUID) → normalized_title (fallback).';

COMMENT ON TABLE matter_appearances IS 'Timeline tracking: Links matters to meetings via agenda items. Enables legislative timeline view showing matter evolution across meetings.';

COMMENT ON TABLE meeting_topics IS 'Normalized from meetings.topics JSON array. Enables efficient topic filtering and indexing.';

COMMENT ON COLUMN meetings.committee_id IS 'FK to committees. A meeting is an occurrence of a committee. Enables meeting → committee navigation.';

COMMENT ON TABLE item_topics IS 'Normalized from items.topics JSON array. Enables efficient topic filtering and indexing.';

COMMENT ON TABLE council_members IS 'Elected officials registry. ID includes city_banana to prevent cross-city collisions. Normalized from city_matters.sponsors JSONB.';

COMMENT ON COLUMN council_members.normalized_name IS 'Lowercase, trimmed name for matching. Handles vendor variations like "John Smith" vs "JOHN SMITH" vs "Smith, John".';

COMMENT ON TABLE sponsorships IS 'Links council members to matters they sponsor. Normalizes city_matters.sponsors JSONB array.';

COMMENT ON TABLE votes IS 'Individual voting records per member per matter per meeting. Same matter may be voted on multiple times across meetings.';

COMMENT ON TABLE committees IS 'Committee registry per city. ID includes city_banana to prevent cross-city collisions. Enables committee-level vote analysis.';

COMMENT ON TABLE committee_members IS 'Tracks council member committee assignments. left_at NULL means currently serving. Historical tracking enables time-aware queries.';

COMMENT ON COLUMN matter_appearances.committee_id IS 'FK to committees table. Enables "how did Committee X vote" queries. TEXT committee field preserved for backward compat.';

COMMENT ON COLUMN matter_appearances.vote_outcome IS 'Result of committee vote: passed, failed, tabled, withdrawn, referred, amended, unknown, no_vote.';

COMMENT ON COLUMN matter_appearances.vote_tally IS 'JSON vote counts: {"yes": N, "no": N, "abstain": N, "absent": N}. Populated from votes table.';

COMMENT ON TABLE deliberations IS 'Opinion clustering sessions linked to legislative matters. Citizens submit comments and vote agree/disagree/pass.';

COMMENT ON TABLE deliberation_comments IS 'User-submitted statements. mod_status: 0=pending, 1=approved, -1=hidden. Trusted users auto-approved.';

COMMENT ON TABLE deliberation_votes IS 'Votes on comments: -1=disagree, 0=pass, 1=agree. Vote matrix used for PCA clustering.';

COMMENT ON TABLE deliberation_results IS 'Cached clustering output: 2D positions, cluster assignments, consensus scores. Recomputed on demand.';

COMMENT ON TABLE happening_items IS 'AI-curated rankings of important upcoming agenda items. Populated via autonomous analysis, surfaced in frontend.';
COMMENT ON COLUMN happening_items.reason IS 'One-sentence explanation of why this item matters to residents.';
COMMENT ON COLUMN happening_items.expires_at IS 'Usually meeting_date + 1 day. Used for automatic cleanup of stale items.';

COMMENT ON TABLE session_events IS 'Anonymous user journey events. Linked by IP hash (same as rate limiting). 7-day retention via cleanup_old_session_events().';
COMMENT ON COLUMN session_events.ip_hash IS 'SHA256[:16] hash of client IP. Same hash used in rate limiting middleware.';

-- =======================
-- REVISION CAPTURE (see migrations/023 for rationale)
-- =======================
-- Re-syncs overwrite meeting/item fields; these triggers record what
-- changed. Frozen items (summary set) never diff — the upsert's freeze
-- CASEs keep NEW = OLD, so no row is written.

CREATE TABLE IF NOT EXISTS meeting_revisions (
    id BIGSERIAL PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    changes JSONB NOT NULL  -- {field: {old, new}}, watched fields only
);
CREATE INDEX IF NOT EXISTS idx_meeting_revisions_meeting ON meeting_revisions(meeting_id);
CREATE INDEX IF NOT EXISTS idx_meeting_revisions_changed ON meeting_revisions(changed_at);

CREATE OR REPLACE FUNCTION record_meeting_revision() RETURNS trigger AS $$
DECLARE
    diff JSONB := '{}'::jsonb;
BEGIN
    IF NEW.title IS DISTINCT FROM OLD.title THEN
        diff := diff || jsonb_build_object('title',
            jsonb_build_object('old', OLD.title, 'new', NEW.title));
    END IF;
    IF NEW.date IS DISTINCT FROM OLD.date THEN
        diff := diff || jsonb_build_object('date',
            jsonb_build_object('old', OLD.date, 'new', NEW.date));
    END IF;
    IF NEW.agenda_url IS DISTINCT FROM OLD.agenda_url THEN
        diff := diff || jsonb_build_object('agenda_url',
            jsonb_build_object('old', OLD.agenda_url, 'new', NEW.agenda_url));
    END IF;
    IF NEW.packet_url IS DISTINCT FROM OLD.packet_url THEN
        diff := diff || jsonb_build_object('packet_url',
            jsonb_build_object('old', OLD.packet_url, 'new', NEW.packet_url));
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        diff := diff || jsonb_build_object('status',
            jsonb_build_object('old', OLD.status, 'new', NEW.status));
    END IF;
    IF diff <> '{}'::jsonb THEN
        INSERT INTO meeting_revisions (meeting_id, changes) VALUES (NEW.id, diff);
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_meeting_revisions ON meetings;
CREATE TRIGGER trg_meeting_revisions
    AFTER UPDATE OF title, date, agenda_url, packet_url, status ON meetings
    FOR EACH ROW EXECUTE FUNCTION record_meeting_revision();

CREATE TABLE IF NOT EXISTS item_revisions (
    id BIGSERIAL PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    meeting_id TEXT NOT NULL,  -- denormalized for meeting-history queries
    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    changes JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_item_revisions_meeting ON item_revisions(meeting_id);

CREATE OR REPLACE FUNCTION record_item_revision() RETURNS trigger AS $$
DECLARE
    diff JSONB := '{}'::jsonb;
BEGIN
    IF NEW.title IS DISTINCT FROM OLD.title THEN
        diff := diff || jsonb_build_object('title',
            jsonb_build_object('old', OLD.title, 'new', NEW.title));
    END IF;
    IF NEW.agenda_number IS DISTINCT FROM OLD.agenda_number THEN
        diff := diff || jsonb_build_object('agenda_number',
            jsonb_build_object('old', OLD.agenda_number, 'new', NEW.agenda_number));
    END IF;
    IF NEW.attachment_hash IS DISTINCT FROM OLD.attachment_hash THEN
        diff := diff || jsonb_build_object('attachment_hash',
            jsonb_build_object('old', OLD.attachment_hash, 'new', NEW.attachment_hash));
    END IF;
    -- body_text can be 15KB+; lengths are the signal, not the payload
    IF NEW.body_text IS DISTINCT FROM OLD.body_text THEN
        diff := diff || jsonb_build_object('body_text',
            jsonb_build_object('old_len', COALESCE(length(OLD.body_text), 0),
                               'new_len', COALESCE(length(NEW.body_text), 0)));
    END IF;
    IF diff <> '{}'::jsonb THEN
        INSERT INTO item_revisions (item_id, meeting_id, changes)
        VALUES (NEW.id, NEW.meeting_id, diff);
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_item_revisions ON items;
CREATE TRIGGER trg_item_revisions
    AFTER UPDATE OF title, agenda_number, attachment_hash, body_text ON items
    FOR EACH ROW EXECUTE FUNCTION record_item_revision();

-- Late additions: items appearing on an already-known agenda inside the
-- notice window (Brown Act: 72h regular, 24h special). The 6h slack
-- excludes multi-pass initial syncs. hours_before_meeting lets consumers
-- pick their own window.
CREATE OR REPLACE VIEW late_additions AS
SELECT
    i.id AS item_id,
    i.meeting_id,
    m.banana,
    m.title AS meeting_title,
    m.date AS meeting_date,
    i.created_at AS item_created_at,
    i.title,
    ROUND((EXTRACT(EPOCH FROM (m.date - i.created_at)) / 3600.0)::numeric, 1)
        AS hours_before_meeting
FROM items i
JOIN meetings m ON m.id = i.meeting_id
WHERE m.date IS NOT NULL
  AND i.created_at > m.created_at + INTERVAL '6 hours'
  AND i.created_at > m.date - INTERVAL '72 hours'
  AND i.created_at < m.date;
