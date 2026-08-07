# Engagic Database Schema

Quick reference for the Engagic PostgreSQL database schema and SQL queries.

**For repository pattern implementation and code examples, see:** [../database/README.md](../database/README.md)

**Database:** PostgreSQL 16 with asyncpg
**Location:** `/opt/engagic` (VPS production)
**Code:** Async Repository Pattern (database/db_postgres.py -> 14 repositories)
**Last Updated:** December 10, 2025

---

**This document provides:**
- Table schemas and column definitions
- JSON structure specifications
- SQL query examples
- Database maintenance commands

**For Python code usage, see:** [../database/README.md](../database/README.md)

---

## Table of Contents

- [Overview](#overview)
- [Core Tables](#core-tables) (cities, zipcodes, meetings, items)
- [Matters Tables](#matters-tables) (city_matters, matter_appearances)
- [Representation Tables](#representation-tables) (committees, council_members, committee_members, votes, sponsorships)
- [Topic Junction Tables](#topic-junction-tables) (item_topics, meeting_topics, matter_topics)
- [Processing Tables](#processing-tables) (cache, queue)
- [Analytics Tables](#analytics-tables) (happening_items, session_events)
- [Deliberation Tables](#deliberation-tables) (deliberations, comments, votes, results)
- [Userland Schema](#userland-schema) (users, alerts, watches, ratings, issues, refresh_tokens)
- [B2B Tables](#b2b-tables) (tenants, tenant_coverage, tenant_keywords, tracked_items)
- [JSON Structures](#json-structures)
- [Indices](#indices)
- [Relationships](#relationships)

---

## Overview

### Database Philosophy

**Single Source of Truth:** One unified PostgreSQL database for all civic data.

**Matters-First Architecture:** Legislative items tracked across meetings with deduplication. Council members and committees tracked for accountability.

**Hybrid Normalization Strategy:** Topics normalized for efficient querying, complex structures (attachments, sponsors) stored as JSONB for flexibility.

**Foreign Keys Enforced:** Referential integrity with CASCADE deletes.

**Connection Pooling:** asyncpg connection pool (5-20 connections) for true concurrent read/write operations.

---

## Design Decisions

### JSONB vs Normalization Strategy

Engagic uses a **hybrid approach** balancing query performance and schema flexibility:

#### Normalized Tables (Separate Tables)

**When to normalize:**
- Fields queried in WHERE clauses (filtering)
- Aggregation queries (COUNT, GROUP BY)
- Simple one-to-many relationships

**Topics (3 normalized tables):**
- `meeting_topics` (meeting_id, topic) - PRIMARY KEY (meeting_id, topic)
- `item_topics` (item_id, topic) - PRIMARY KEY (item_id, topic)
- `matter_topics` (matter_id, topic) - PRIMARY KEY (matter_id, topic)

**Why normalized:**
- Fast filtering: `SELECT * FROM meetings WHERE id IN (SELECT meeting_id FROM meeting_topics WHERE topic = 'Housing')`
- Efficient aggregation: `SELECT topic, COUNT(*) FROM meeting_topics GROUP BY topic`
- GIN indexes on topic columns for full-text search

**Zipcodes (many-to-many):**
- `zipcodes` (banana, zipcode, is_primary) - PRIMARY KEY (banana, zipcode)

**Why normalized:**
- Search by zipcode (user feature)
- Many-to-many relationship (cities have multiple zipcodes)

#### JSONB Fields (Not Normalized)

**When to use JSONB:**
- Complex nested structures (objects within arrays)
- Variable schemas (metadata varies by vendor)
- Rarely queried fields (displayed but not filtered)

**Attachments (JSONB in 2 tables):**
- `items.attachments` - JSONB array: `[{url, title, pages}]`
- `city_matters.attachments` - JSONB array

**Why JSONB:**
- Complex structure: Each attachment has 3 fields (url, title, pages)
- Display-only: Shown to users but rarely filtered
- No query pattern: "Show all items with PDF attachment" not a current requirement

**Sponsors (JSONB):**
- `city_matters.sponsors` - JSONB array: `["Council Member Smith", "Mayor Jones"]`

**Why JSONB:**
- Simple array of strings
- Display-only: Listed on meeting pages
- Actual sponsorship relationships tracked in `sponsorships` table for queries

**Participation (JSONB):**
- `meetings.participation` - JSONB object: `{email, phone, zoom}`

**Why JSONB:**
- Semi-structured data (3 optional fields)
- Display-only: Contact info shown to users
- No filtering: Never queried, just displayed

**Metadata (JSONB):**
- `city_matters.metadata` - Vendor-specific data
- `council_members.metadata` - Additional member info

**Why JSONB:**
- Intentionally flexible (varies by vendor)
- Not queried (internal debugging/diagnostics)

---

## Core Tables

### `cities`

Cities covered by Engagic across 11 vendor platforms.

**Primary Key:** `banana` (vendor-agnostic identifier)

| Column | Type | Description |
|--------|------|-------------|
| `banana` | TEXT PRIMARY KEY | Vendor-agnostic city identifier (e.g., "paloaltoCA") |
| `name` | TEXT NOT NULL | City name (e.g., "Palo Alto") |
| `state` | TEXT NOT NULL | State code (e.g., "CA") |
| `vendor` | TEXT NOT NULL | Platform vendor (legistar, primegov, granicus, etc.) |
| `slug` | TEXT NOT NULL | Vendor-specific identifier |
| `county` | TEXT | County name (optional) |
| `status` | TEXT | City status: 'active', 'inactive' (default: 'active') |
| `participation` | JSONB | City-level participation config: {testimony_url, testimony_email, process_url} |
| `population` | INTEGER | City population (Census data) |
| `geom` | geometry(MultiPolygon, 4326) | City boundary polygon from Census TIGER/Line |
| `created_at` | TIMESTAMP | Record creation timestamp |
| `updated_at` | TIMESTAMP | Last update timestamp |

**Constraints:**
- `UNIQUE(name, state)` - One record per city

**Indices:**
- `idx_cities_vendor` on `vendor`
- `idx_cities_state` on `state`
- `idx_cities_status` on `status`
- `idx_cities_population` on `population DESC NULLS LAST`
- `idx_cities_geom` GIST index on `geom` (spatial queries)

---

### `zipcodes`

Many-to-many relationship between cities and zipcodes.

**Primary Key:** Composite `(banana, zipcode)`

| Column | Type | Description |
|--------|------|-------------|
| `banana` | TEXT NOT NULL | City identifier (FK to cities) |
| `zipcode` | TEXT NOT NULL | 5-digit zipcode |
| `is_primary` | BOOLEAN | Primary zipcode for this city (default: FALSE) |

**Constraints:**
- `FOREIGN KEY (banana) REFERENCES cities(banana) ON DELETE CASCADE`

**Indices:**
- `idx_zipcodes_zipcode` on `zipcode`

---

### `meetings`

Meeting records with optional summaries and metadata.

**Primary Key:** `id` (generated meeting ID)

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PRIMARY KEY | Unique meeting identifier |
| `banana` | TEXT NOT NULL | City identifier (FK to cities) |
| `title` | TEXT NOT NULL | Meeting title |
| `date` | TIMESTAMP | Meeting date/time |
| `agenda_url` | TEXT | URL to HTML agenda page (item-based, primary) |
| `agenda_sources` | JSONB | Multi-agenda provenance: [{type, url, label}] (PrimeGov) |
| `packet_url` | TEXT | URL to PDF packet (monolithic, fallback) |
| `summary` | TEXT | LLM-generated meeting summary (markdown) |
| `participation` | JSONB | Contact info: email, phone, virtual_url, is_hybrid |
| `status` | TEXT | Meeting status (scheduled, completed, etc.) |
| `committee_id` | TEXT | FK to committees (if meeting belongs to a committee) |
| `processing_status` | TEXT | Processing state: 'pending', 'completed', 'failed' |
| `processing_method` | TEXT | How processed: 'item-based', 'monolithic', 'batch' |
| `processing_time` | REAL | Processing duration in seconds |
| `search_vector` | tsvector | Generated column for optimized full-text search |
| `created_at` | TIMESTAMP | Record creation timestamp |
| `updated_at` | TIMESTAMP | Last update timestamp |

**Constraints:**
- `FOREIGN KEY (banana) REFERENCES cities(banana) ON DELETE CASCADE`
- `FOREIGN KEY (committee_id) REFERENCES committees(id) ON DELETE SET NULL`

**Indices:**
- `idx_meetings_banana` on `banana`
- `idx_meetings_date` on `date`
- `idx_meetings_status` on `processing_status`
- `idx_meetings_committee` on `committee_id`
- `idx_meetings_search_vector` GIN index on `search_vector`

---

### `items` (agenda_items)

Individual agenda items within meetings.

**Primary Key:** `id` (generated item identifier)

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PRIMARY KEY | Unique item identifier |
| `meeting_id` | TEXT NOT NULL | Parent meeting ID (FK to meetings) |
| `matter_id` | TEXT | FK to city_matters (if item tracks a matter) |
| `matter_file` | TEXT | Denormalized file number for query performance |
| `matter_type` | TEXT | Denormalized matter type |
| `title` | TEXT NOT NULL | Agenda item title |
| `agenda_number` | TEXT | Agenda item number (e.g., "5.A") |
| `sequence` | INTEGER NOT NULL | Order within agenda (1, 2, 3...) |
| `attachments` | JSONB | Array of attachment objects |
| `attachment_hash` | TEXT | SHA-256 hash of attachments for change detection |
| `sponsors` | JSONB | Array of sponsor names |
| `topics` | JSONB | Array of topic identifiers (also normalized to item_topics) |
| `summary` | TEXT | LLM-generated item summary (markdown) |
| `search_vector` | tsvector | Generated column for optimized full-text search |
| `quality_score` | REAL | Denormalized from ratings for efficient queries |
| `rating_count` | INTEGER | Number of ratings received (default: 0) |
| `created_at` | TIMESTAMP | Record creation timestamp |

**Constraints:**
- `FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE`
- `FOREIGN KEY (matter_id) REFERENCES city_matters(id) ON DELETE SET NULL`

**Indices:**
- `idx_items_meeting_id` on `meeting_id`
- `idx_items_matter_id` on `matter_id`
- `idx_items_matter_file` on `matter_file`
- `idx_items_meeting_sequence` on `(meeting_id, sequence)`
- `idx_items_search_vector` GIN index on `search_vector`

---

## Matters Tables

Legislative items (bills, ordinances, resolutions) tracked across meetings.

### `city_matters`

Core matters table - legislative items deduplicated across meetings.

**Primary Key:** `id` (generated matter ID)

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PRIMARY KEY | Unique matter identifier |
| `banana` | TEXT NOT NULL | City identifier (FK to cities) |
| `matter_id` | TEXT | Vendor's original matter ID |
| `matter_file` | TEXT | File number (e.g., "ORD-2025-001") |
| `matter_type` | TEXT | Type: ordinance, resolution, motion, etc. |
| `title` | TEXT NOT NULL | Matter title |
| `sponsors` | JSONB | Array of sponsor names (display) |
| `canonical_summary` | TEXT | LLM-generated canonical summary |
| `canonical_topics` | JSONB | Array of canonical topic IDs |
| `attachments` | JSONB | Array of attachment objects |
| `metadata` | JSONB | Vendor-specific metadata |
| `first_seen` | TIMESTAMP | First appearance date |
| `last_seen` | TIMESTAMP | Most recent appearance date |
| `appearance_count` | INTEGER | Number of meetings this matter appeared in |
| `status` | TEXT | Status: active, passed, failed, tabled, withdrawn, referred, amended, vetoed, enacted |
| `quality_score` | REAL | Summary quality rating (user feedback) |
| `rating_count` | INTEGER | Number of ratings received |
| `final_vote_date` | TIMESTAMP | Date of final vote (if any) |
| `search_vector` | tsvector | Generated column for optimized full-text search |
| `created_at` | TIMESTAMP | Record creation timestamp |
| `updated_at` | TIMESTAMP | Last update timestamp |

**Constraints:**
- `FOREIGN KEY (banana) REFERENCES cities(banana) ON DELETE CASCADE`
- `CHECK (status IN ('active', 'passed', 'failed', 'tabled', 'withdrawn', 'referred', 'amended', 'vetoed', 'enacted'))`

**Indices:**
- `idx_city_matters_banana` on `banana`
- `idx_city_matters_status` on `status`
- `idx_city_matters_matter_file` on `matter_file`
- `idx_city_matters_first_seen` on `first_seen`
- `idx_city_matters_final_vote` on `final_vote_date`
- `idx_city_matters_search_vector` GIN index on `search_vector`

---

### `matter_appearances`

Tracks each time a matter appears in a meeting (junction table).

**Primary Key:** `id` (auto-increment)

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGINT PRIMARY KEY | Auto-increment ID |
| `matter_id` | TEXT NOT NULL | FK to city_matters |
| `meeting_id` | TEXT NOT NULL | FK to meetings |
| `item_id` | TEXT NOT NULL | FK to items |
| `appeared_at` | TIMESTAMP | Authoritative meeting date; NULL for undated meetings |
| `committee` | TEXT | Committee name (denormalized) |
| `committee_id` | TEXT | FK to committees |
| `action` | TEXT | Action taken (e.g., "First Reading") |
| `sequence` | INTEGER | Order in legislative process |
| `vote_outcome` | TEXT | Outcome: passed, failed, tabled, withdrawn, referred, amended, unknown, no_vote |
| `vote_tally` | JSONB | Vote counts: {yes, no, abstain, absent} |
| `created_at` | TIMESTAMP | Record creation timestamp |

**Constraints:**
- `FOREIGN KEY (matter_id) REFERENCES city_matters(id) ON DELETE CASCADE`
- `FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE`
- `FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE`
- `FOREIGN KEY (committee_id) REFERENCES committees(id) ON DELETE SET NULL`
- `UNIQUE (matter_id, meeting_id, item_id)`
- `CHECK (vote_outcome IN ('passed', 'failed', 'tabled', 'withdrawn', 'referred', 'amended', 'unknown', 'no_vote'))`

**Indices:**
- `idx_matter_appearances_matter` on `matter_id`
- `idx_matter_appearances_meeting` on `meeting_id`
- `idx_matter_appearances_item` on `item_id`
- `idx_matter_appearances_committee_id` on `committee_id`
- `idx_matter_appearances_outcome` on `vote_outcome`
- `idx_matter_appearances_date` on `appeared_at`

---

## Representation Tables

Council members, committees, and voting records for legislative accountability.

### `council_members`

Elected officials tracked across cities.

**Primary Key:** `id` (generated member ID)

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PRIMARY KEY | Unique member identifier |
| `banana` | TEXT NOT NULL | City identifier (FK to cities) |
| `name` | TEXT NOT NULL | Display name |
| `normalized_name` | TEXT NOT NULL | Normalized for matching (lowercase, no titles) |
| `title` | TEXT | Title (Mayor, Council Member, etc.) |
| `district` | TEXT | District/ward represented |
| `status` | TEXT | Status: active, former, unknown |
| `first_seen` | TIMESTAMP | First appearance in data |
| `last_seen` | TIMESTAMP | Most recent appearance |
| `sponsorship_count` | INTEGER | Number of matters sponsored |
| `vote_count` | INTEGER | Number of votes cast |
| `metadata` | JSONB | Additional info (party, email, etc.) |
| `created_at` | TIMESTAMP | Record creation timestamp |
| `updated_at` | TIMESTAMP | Last update timestamp |

**Constraints:**
- `FOREIGN KEY (banana) REFERENCES cities(banana) ON DELETE CASCADE`
- `UNIQUE (banana, normalized_name)`
- `CHECK (status IN ('active', 'former', 'unknown'))`

**Indices:**
- `idx_council_members_banana` on `banana`
- `idx_council_members_status` on `status`
- `idx_council_members_normalized` on `normalized_name`
- `idx_council_members_banana_status` on `(banana, status)`
- `idx_council_members_fts` GIN index for full-text search on name

---

### `committees`

Legislative bodies within cities (Planning Commission, Budget Committee, etc.).

**Primary Key:** `id` (generated committee ID)

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PRIMARY KEY | Unique committee identifier |
| `banana` | TEXT NOT NULL | City identifier (FK to cities) |
| `name` | TEXT NOT NULL | Display name |
| `normalized_name` | TEXT NOT NULL | Normalized for matching |
| `description` | TEXT | Committee purpose/scope |
| `status` | TEXT | Status: active, inactive, unknown |
| `created_at` | TIMESTAMP | Record creation timestamp |
| `updated_at` | TIMESTAMP | Last update timestamp |

**Constraints:**
- `FOREIGN KEY (banana) REFERENCES cities(banana) ON DELETE CASCADE`
- `UNIQUE (banana, normalized_name)`
- `CHECK (status IN ('active', 'inactive', 'unknown'))`

**Indices:**
- `idx_committees_banana` on `banana`
- `idx_committees_status` on `status`
- `idx_committees_name` on `normalized_name`
- `idx_committees_fts` GIN index for full-text search on name

---

### `committee_members`

Junction table: which council members serve on which committees.

**Primary Key:** `id` (auto-increment)

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGINT PRIMARY KEY | Auto-increment ID |
| `committee_id` | TEXT NOT NULL | FK to committees |
| `council_member_id` | TEXT NOT NULL | FK to council_members |
| `role` | TEXT | Role on committee (Chair, Vice Chair, Member) |
| `joined_at` | TIMESTAMP | When member joined committee |
| `left_at` | TIMESTAMP | When member left (NULL if current) |
| `created_at` | TIMESTAMP | Record creation timestamp |

**Constraints:**
- `FOREIGN KEY (committee_id) REFERENCES committees(id) ON DELETE CASCADE`
- `FOREIGN KEY (council_member_id) REFERENCES council_members(id) ON DELETE CASCADE`
- `UNIQUE (committee_id, council_member_id, joined_at)`

**Indices:**
- `idx_committee_members_committee` on `committee_id`
- `idx_committee_members_member` on `council_member_id`
- `idx_committee_members_active` on `committee_id` WHERE `left_at IS NULL`
- `idx_committee_members_dates` on `(joined_at, left_at)`

---

### `votes`

Individual vote records for each council member on each matter.

**Primary Key:** `id` (auto-increment)

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGINT PRIMARY KEY | Auto-increment ID |
| `council_member_id` | TEXT NOT NULL | FK to council_members |
| `matter_id` | TEXT NOT NULL | FK to city_matters |
| `meeting_id` | TEXT NOT NULL | FK to meetings |
| `vote` | TEXT NOT NULL | Vote cast: yes, no, abstain, absent, present, recused, not_voting |
| `vote_date` | TIMESTAMP | When vote was cast |
| `sequence` | INTEGER | Order in voting sequence |
| `metadata` | JSONB | Additional vote info |
| `created_at` | TIMESTAMP | Record creation timestamp |

**Constraints:**
- `FOREIGN KEY (council_member_id) REFERENCES council_members(id) ON DELETE CASCADE`
- `FOREIGN KEY (matter_id) REFERENCES city_matters(id) ON DELETE CASCADE`
- `FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE`
- `UNIQUE (council_member_id, matter_id, meeting_id)`
- `CHECK (vote IN ('yes', 'no', 'abstain', 'absent', 'present', 'recused', 'not_voting'))`

**Indices:**
- `idx_votes_member` on `council_member_id`
- `idx_votes_matter` on `matter_id`
- `idx_votes_meeting` on `meeting_id`
- `idx_votes_value` on `vote`
- `idx_votes_member_date` on `(council_member_id, vote_date DESC)`

---

### `sponsorships`

Which council members sponsored which matters.

**Primary Key:** `id` (auto-increment)

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGINT PRIMARY KEY | Auto-increment ID |
| `council_member_id` | TEXT NOT NULL | FK to council_members |
| `matter_id` | TEXT NOT NULL | FK to city_matters |
| `is_primary` | BOOLEAN | Primary sponsor (vs. co-sponsor) |
| `sponsor_order` | INTEGER | Order among sponsors |
| `created_at` | TIMESTAMP | Record creation timestamp |

**Constraints:**
- `FOREIGN KEY (council_member_id) REFERENCES council_members(id) ON DELETE CASCADE`
- `FOREIGN KEY (matter_id) REFERENCES city_matters(id) ON DELETE CASCADE`
- `UNIQUE (council_member_id, matter_id)`

**Indices:**
- `idx_sponsorships_member` on `council_member_id`
- `idx_sponsorships_matter` on `matter_id`
- `idx_sponsorships_primary` on `is_primary` WHERE `is_primary = true`

---

## Topic Junction Tables

Normalized topic relationships for efficient filtering.

### `item_topics`

Topics associated with agenda items.

**Primary Key:** Composite `(item_id, topic)`

| Column | Type | Description |
|--------|------|-------------|
| `item_id` | TEXT NOT NULL | FK to items |
| `topic` | TEXT NOT NULL | Canonical topic identifier |

**Constraints:**
- `FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE`

**Indices:**
- `idx_item_topics_item` on `item_id`
- `idx_item_topics_topic` on `topic`

---

### `meeting_topics`

Topics associated with meetings (aggregated from items).

**Primary Key:** Composite `(meeting_id, topic)`

| Column | Type | Description |
|--------|------|-------------|
| `meeting_id` | TEXT NOT NULL | FK to meetings |
| `topic` | TEXT NOT NULL | Canonical topic identifier |

**Constraints:**
- `FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE`

**Indices:**
- `idx_meeting_topics_meeting` on `meeting_id`
- `idx_meeting_topics_topic` on `topic`

---

### `matter_topics`

Topics associated with matters.

**Primary Key:** Composite `(matter_id, topic)`

| Column | Type | Description |
|--------|------|-------------|
| `matter_id` | TEXT NOT NULL | FK to city_matters |
| `topic` | TEXT NOT NULL | Canonical topic identifier |

**Constraints:**
- `FOREIGN KEY (matter_id) REFERENCES city_matters(id) ON DELETE CASCADE`

**Indices:**
- `idx_matter_topics_matter` on `matter_id`
- `idx_matter_topics_topic` on `topic`

---

## Processing Tables

### `cache`

Processing cache for cost optimization and deduplication.

**Primary Key:** `packet_url`

| Column | Type | Description |
|--------|------|-------------|
| `packet_url` | TEXT PRIMARY KEY | PDF URL (cache key) |
| `content_hash` | TEXT | SHA-256 hash of PDF content |
| `processing_method` | TEXT | Method used: 'gemini', 'fallback' |
| `processing_time` | REAL | Processing duration in seconds |
| `cache_hit_count` | INTEGER | Number of cache hits (default: 0) |
| `created_at` | TIMESTAMP | First processing timestamp |
| `last_accessed` | TIMESTAMP | Most recent access timestamp |

**Indices:**
- `idx_cache_hash` on `content_hash`

**Purpose:** Avoid reprocessing identical PDFs across meetings.

---

### `queue`

Processing job queue with priority support.

**Primary Key:** `id` (auto-increment)

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGSERIAL PRIMARY KEY | Auto-increment job ID |
| `source_url` | TEXT NOT NULL UNIQUE | Vendor-agnostic URL to process |
| `meeting_id` | TEXT | Associated meeting ID (FK to meetings) |
| `banana` | TEXT | City identifier (FK to cities) |
| `job_type` | TEXT | Type of processing job |
| `payload` | JSONB | Job-specific payload data |
| `status` | TEXT | Job status: pending, processing, completed, failed, dead_letter |
| `priority` | INTEGER | Priority score (higher = more urgent, default: 0) |
| `retry_count` | INTEGER | Number of retry attempts (default: 0) |
| `created_at` | TIMESTAMP | Job creation timestamp |
| `started_at` | TIMESTAMP | Processing start timestamp |
| `completed_at` | TIMESTAMP | Processing completion timestamp |
| `failed_at` | TIMESTAMP | Failure timestamp |
| `error_message` | TEXT | Failure details if status='failed' |
| `processing_metadata` | JSONB | Processing metadata (timing, method, etc.) |

**Constraints:**
- `FOREIGN KEY (banana) REFERENCES cities(banana) ON DELETE CASCADE`
- `FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE`
- `CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'dead_letter'))`

**Indices:**
- `idx_queue_status` on `status`
- `idx_queue_city` on `banana`
- `idx_queue_processing` on `(status, priority DESC, created_at ASC)`

**Priority Calculation:**
```python
# Recent meetings get higher priority (0-150 scale)
days_from_meeting = abs((now - meeting_date).days)
priority = max(0, 150 - days_from_meeting)

# Retry jobs get priority penalty
if retry_count > 0:
    priority -= (20 * retry_count)
```

---

## Analytics Tables

Tables for user journey tracking and AI-curated content.

### `happening_items`

AI-curated rankings of important upcoming agenda items. Populated via autonomous analysis, surfaced in "Happening This Week" frontend section.

**Primary Key:** `id` (auto-increment)

| Column | Type | Description |
|--------|------|-------------|
| `id` | SERIAL PRIMARY KEY | Auto-increment ID |
| `banana` | TEXT NOT NULL | City identifier (FK to cities) |
| `item_id` | TEXT NOT NULL | Agenda item ID (FK to items) |
| `meeting_id` | TEXT NOT NULL | Meeting ID (FK to meetings) |
| `meeting_date` | TIMESTAMP NOT NULL | Meeting date for filtering |
| `rank` | INTEGER NOT NULL | Importance ranking (1 = most important) |
| `reason` | TEXT NOT NULL | One-sentence explanation of why this item matters |
| `created_at` | TIMESTAMP | Record creation timestamp |
| `expires_at` | TIMESTAMP NOT NULL | Auto-cleanup timestamp (usually meeting_date + 1 day) |

**Constraints:**
- `FOREIGN KEY (banana) REFERENCES cities(banana) ON DELETE CASCADE`
- `FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE`
- `FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE`
- `UNIQUE (banana, item_id)`

**Indices:**
- `idx_happening_banana_expires` on `(banana, expires_at)`
- `idx_happening_expires` on `expires_at`

---

### `session_events`

Anonymous user journey events for analytics. Linked by IP hash (same as rate limiting). 7-day retention via cleanup function.

**Primary Key:** `id` (auto-increment)

| Column | Type | Description |
|--------|------|-------------|
| `id` | SERIAL PRIMARY KEY | Auto-increment ID |
| `ip_hash` | TEXT NOT NULL | SHA256[:16] hash of client IP |
| `event` | TEXT NOT NULL | Event type (page_view, search, etc.) |
| `url` | TEXT | Page URL |
| `properties` | JSONB | Event-specific metadata |
| `created_at` | TIMESTAMPTZ | Event timestamp |

**Indices:**
- `idx_session_events_ip_hash` on `(ip_hash, created_at DESC)`
- `idx_session_events_created` on `created_at DESC`

**Cleanup:** `cleanup_old_session_events()` function deletes events older than 7 days.

---

## Deliberation Tables

Opinion clustering system for civic engagement. Citizens submit comments on matters and vote agree/disagree/pass.

### `deliberations`

Deliberation sessions linked to legislative matters.

**Primary Key:** `id`

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PRIMARY KEY | Format: "delib_{matter_id}_{short_hash}" |
| `matter_id` | TEXT NOT NULL | FK to city_matters |
| `banana` | TEXT NOT NULL | City identifier |
| `topic` | TEXT | Optional override of matter title |
| `is_active` | BOOLEAN | Whether deliberation is open (default: true) |
| `created_at` | TIMESTAMPTZ | Creation timestamp |
| `closed_at` | TIMESTAMPTZ | When deliberation was closed |

**Constraints:**
- `FOREIGN KEY (matter_id) REFERENCES city_matters(id) ON DELETE CASCADE`

**Indices:**
- `idx_delib_matter` on `matter_id`
- `idx_delib_banana` on `banana`

---

### `deliberation_participants`

Tracks participant numbers for pseudonymous display.

**Primary Key:** Composite `(deliberation_id, user_id)`

| Column | Type | Description |
|--------|------|-------------|
| `deliberation_id` | TEXT NOT NULL | FK to deliberations |
| `user_id` | TEXT NOT NULL | FK to userland.users |
| `participant_number` | INTEGER NOT NULL | Sequential number for pseudonym |
| `joined_at` | TIMESTAMPTZ | When user joined |

---

### `deliberation_comments`

User-submitted statements for clustering.

**Primary Key:** `id` (auto-increment)

| Column | Type | Description |
|--------|------|-------------|
| `id` | SERIAL PRIMARY KEY | Auto-increment ID |
| `deliberation_id` | TEXT NOT NULL | FK to deliberations |
| `user_id` | TEXT NOT NULL | FK to userland.users |
| `participant_number` | INTEGER NOT NULL | Pseudonym: "Participant 1", "Participant 2" |
| `txt` | TEXT NOT NULL | Comment text |
| `mod_status` | INTEGER | Moderation: 0=pending, 1=approved, -1=hidden |
| `created_at` | TIMESTAMPTZ | Submission timestamp |

**Indices:**
- `idx_delib_comments_delib` on `deliberation_id`
- `idx_delib_comments_mod` on `(deliberation_id, mod_status)`
- `idx_delib_comments_unique` UNIQUE on `(deliberation_id, user_id, txt)`

---

### `deliberation_votes`

Votes on comments for opinion clustering.

**Primary Key:** Composite `(comment_id, user_id)`

| Column | Type | Description |
|--------|------|-------------|
| `comment_id` | INTEGER NOT NULL | FK to deliberation_comments |
| `user_id` | TEXT NOT NULL | FK to userland.users |
| `vote` | SMALLINT NOT NULL | -1=disagree, 0=pass, 1=agree |
| `created_at` | TIMESTAMPTZ | Vote timestamp |

**Constraints:**
- `CHECK (vote IN (-1, 0, 1))`

**Indices:**
- `idx_delib_votes_comment` on `comment_id`

---

### `deliberation_results`

Cached clustering output. Recomputed on demand.

**Primary Key:** `deliberation_id`

| Column | Type | Description |
|--------|------|-------------|
| `deliberation_id` | TEXT PRIMARY KEY | FK to deliberations |
| `n_participants` | INTEGER NOT NULL | Number of participants |
| `n_comments` | INTEGER NOT NULL | Number of comments |
| `k` | INTEGER NOT NULL | Number of clusters |
| `positions` | JSONB | 2D positions per participant: [[x,y], ...] |
| `clusters` | JSONB | Cluster assignments: {user_id: cluster_id} |
| `cluster_centers` | JSONB | Cluster centers: [[x,y], ...] |
| `consensus` | JSONB | Consensus scores: {comment_id: score} |
| `group_votes` | JSONB | Per-cluster vote tallies |
| `computed_at` | TIMESTAMPTZ | When results were computed |

---

## Userland Schema

User authentication, engagement, and feedback system. All tables in the `userland` schema namespace.

### `userland.users`

User accounts for authentication and engagement features.

**Primary Key:** `id`

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PRIMARY KEY | User identifier (UUID) |
| `name` | TEXT NOT NULL | Display name |
| `email` | TEXT UNIQUE NOT NULL | User email address |
| `created_at` | TIMESTAMP | Account creation timestamp |
| `last_login` | TIMESTAMP | Most recent login timestamp |

**Indices:**
- `idx_userland_users_email` on `email`

---

### `userland.alerts`

User-configured alerts for meeting/item notifications.

**Primary Key:** `id`

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PRIMARY KEY | Alert identifier |
| `user_id` | TEXT NOT NULL | FK to users |
| `name` | TEXT NOT NULL | Alert name |
| `cities` | JSONB NOT NULL | Array of city bananas to monitor |
| `criteria` | JSONB NOT NULL | Matching criteria: {keywords: [...]} |
| `frequency` | TEXT | Notification frequency: 'weekly', 'daily' |
| `active` | BOOLEAN | Whether alert is active (default: TRUE) |
| `created_at` | TIMESTAMP | Creation timestamp |

**Constraints:**
- `FOREIGN KEY (user_id) REFERENCES userland.users(id) ON DELETE CASCADE`
- `CHECK (frequency IN ('weekly', 'daily'))`

**Indices:**
- `idx_userland_alerts_user` on `user_id`
- `idx_userland_alerts_active` on `active`
- `idx_userland_alerts_criteria` GIN index on `criteria`

---

### `userland.alert_matches`

Matched meetings/items that triggered user alerts.

**Primary Key:** `id`

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PRIMARY KEY | Match identifier |
| `alert_id` | TEXT NOT NULL | FK to alerts |
| `meeting_id` | TEXT NOT NULL | Meeting that matched |
| `item_id` | TEXT | Item that matched (NULL for meeting-level) |
| `match_type` | TEXT NOT NULL | Type: 'keyword', 'matter' |
| `confidence` | REAL NOT NULL | Match confidence 0.0-1.0 |
| `matched_criteria` | JSONB NOT NULL | Match details for display |
| `notified` | BOOLEAN | Whether user was notified (default: FALSE) |
| `created_at` | TIMESTAMP | Match timestamp |

**Constraints:**
- `FOREIGN KEY (alert_id) REFERENCES userland.alerts(id) ON DELETE CASCADE`
- `CHECK (match_type IN ('keyword', 'matter'))`
- `CHECK (confidence >= 0 AND confidence <= 1)`

---

### `userland.watches`

User watchlist for entities (matters, meetings, topics, cities, council members).

**Primary Key:** `id`

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGSERIAL PRIMARY KEY | Auto-increment ID |
| `user_id` | TEXT NOT NULL | FK to users |
| `entity_type` | TEXT NOT NULL | Type: matter, meeting, topic, city, council_member |
| `entity_id` | TEXT NOT NULL | Entity identifier |
| `created_at` | TIMESTAMP | Watch creation timestamp |

**Constraints:**
- `FOREIGN KEY (user_id) REFERENCES userland.users(id) ON DELETE CASCADE`
- `UNIQUE (user_id, entity_type, entity_id)`
- `CHECK (entity_type IN ('matter', 'meeting', 'topic', 'city', 'council_member'))`

---

### `userland.activity_log`

User activity tracking for analytics and trending calculations.

**Primary Key:** `id`

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGSERIAL PRIMARY KEY | Auto-increment ID |
| `user_id` | TEXT | FK to users (NULL for anonymous) |
| `session_id` | TEXT | Session ID for anonymous tracking |
| `action` | TEXT NOT NULL | Action: view, watch, unwatch, search, share, rate, report |
| `entity_type` | TEXT NOT NULL | Entity type |
| `entity_id` | TEXT | Entity identifier |
| `metadata` | JSONB | Action-specific metadata |
| `created_at` | TIMESTAMP | Event timestamp |

**Constraints:**
- `CHECK (action IN ('view', 'watch', 'unwatch', 'search', 'share', 'rate', 'report'))`

---

### `userland.ratings`

User ratings (1-5 stars) for items, meetings, and matters.

**Primary Key:** `id`

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGSERIAL PRIMARY KEY | Auto-increment ID |
| `user_id` | TEXT | FK to users (NULL for anonymous) |
| `session_id` | TEXT | Session ID for anonymous rating |
| `entity_type` | TEXT NOT NULL | Type: item, meeting, matter |
| `entity_id` | TEXT NOT NULL | Entity identifier |
| `rating` | SMALLINT NOT NULL | Rating 1-5 |
| `created_at` | TIMESTAMP | Rating timestamp |

**Constraints:**
- `UNIQUE (user_id, entity_type, entity_id)`
- `CHECK (entity_type IN ('item', 'meeting', 'matter'))`
- `CHECK (rating BETWEEN 1 AND 5)`

---

### `userland.issues`

User-reported issues for admin review.

**Primary Key:** `id`

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGSERIAL PRIMARY KEY | Auto-increment ID |
| `user_id` | TEXT | FK to users |
| `session_id` | TEXT | Session ID for anonymous reports |
| `entity_type` | TEXT NOT NULL | Entity type |
| `entity_id` | TEXT NOT NULL | Entity identifier |
| `issue_type` | TEXT NOT NULL | Type: inaccurate, incomplete, misleading, offensive, other |
| `description` | TEXT NOT NULL | Issue description |
| `status` | TEXT | Status: open, resolved, dismissed |
| `admin_notes` | TEXT | Admin notes |
| `created_at` | TIMESTAMP | Report timestamp |
| `resolved_at` | TIMESTAMP | Resolution timestamp |

**Constraints:**
- `CHECK (issue_type IN ('inaccurate', 'incomplete', 'misleading', 'offensive', 'other'))`
- `CHECK (status IN ('open', 'resolved', 'dismissed'))`

---

### `userland.used_magic_links`

Security table to prevent magic link replay attacks.

**Primary Key:** `token_hash`

| Column | Type | Description |
|--------|------|-------------|
| `token_hash` | TEXT PRIMARY KEY | SHA-256 hash of used token |
| `user_id` | TEXT NOT NULL | FK to users |
| `used_at` | TIMESTAMP | When token was used |
| `expires_at` | TIMESTAMP NOT NULL | Token expiration time |

**Constraints:**
- `FOREIGN KEY (user_id) REFERENCES userland.users(id) ON DELETE CASCADE`

---

### `userland.refresh_tokens`

Server-side refresh token storage for revocation support.

**Primary Key:** `token_hash`

| Column | Type | Description |
|--------|------|-------------|
| `token_hash` | TEXT PRIMARY KEY | SHA-256 hash of token |
| `user_id` | TEXT NOT NULL | FK to users |
| `created_at` | TIMESTAMPTZ | Token creation timestamp |
| `expires_at` | TIMESTAMPTZ NOT NULL | Token expiration time |
| `revoked_at` | TIMESTAMPTZ | When token was revoked (NULL = active) |
| `revoked_reason` | TEXT | Revocation reason: logout, rotation, security |

**Constraints:**
- `FOREIGN KEY (user_id) REFERENCES userland.users(id) ON DELETE CASCADE`

---

### `userland.city_requests`

Track unknown cities users request for coverage expansion.

**Primary Key:** `city_banana`

| Column | Type | Description |
|--------|------|-------------|
| `city_banana` | TEXT PRIMARY KEY | Requested city identifier |
| `request_count` | INTEGER | Number of requests (default: 1) |
| `first_requested` | TIMESTAMP | First request timestamp |
| `last_requested` | TIMESTAMP | Most recent request |
| `status` | TEXT | Status: pending, added, rejected |
| `notes` | TEXT | Admin notes |

**Constraints:**
- `CHECK (status IN ('pending', 'added', 'rejected'))`

---

### `userland.deliberation_trusted_users`

Trusted users who have had deliberation comments approved. Auto-approve future submissions.

**Primary Key:** `user_id`

| Column | Type | Description |
|--------|------|-------------|
| `user_id` | TEXT PRIMARY KEY | FK to users |
| `first_approved_at` | TIMESTAMPTZ | When first comment was approved |

**Constraints:**
- `FOREIGN KEY (user_id) REFERENCES userland.users(id) ON DELETE CASCADE`

---

### `userland.trending_matters` (Materialized View)

Trending matters based on user engagement. Refreshed every 15 minutes.

| Column | Type | Description |
|--------|------|-------------|
| `matter_id` | TEXT | Matter identifier |
| `engagement` | BIGINT | Total engagement count |
| `unique_users` | BIGINT | Unique user/session count |

---

## B2B Tables

Tables for B2B API access (implemented, not yet active).

### `tenants`

B2B customers for paid API access.

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PRIMARY KEY | Tenant identifier (UUID) |
| `name` | TEXT NOT NULL | Organization name |
| `api_key` | TEXT UNIQUE NOT NULL | API authentication key |
| `webhook_url` | TEXT | Webhook endpoint for notifications |
| `created_at` | TIMESTAMP | Account creation timestamp |

---

### `tenant_coverage`

Cities each tenant tracks.

| Column | Type | Description |
|--------|------|-------------|
| `tenant_id` | TEXT NOT NULL | Tenant ID (FK to tenants) |
| `banana` | TEXT NOT NULL | City identifier (FK to cities) |
| `added_at` | TIMESTAMP | When city was added to coverage |

**Primary Key:** Composite `(tenant_id, banana)`

**Constraints:**
- `FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE`
- `FOREIGN KEY (banana) REFERENCES cities(banana) ON DELETE CASCADE`

---

### `tenant_keywords`

Topics/keywords tenants care about.

| Column | Type | Description |
|--------|------|-------------|
| `tenant_id` | TEXT NOT NULL | Tenant ID (FK to tenants) |
| `keyword` | TEXT NOT NULL | Search keyword or topic |
| `added_at` | TIMESTAMP | When keyword was added |

**Primary Key:** Composite `(tenant_id, keyword)`

**Constraints:**
- `FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE`

---

### `tracked_items`

Tenant-tracked ordinances and proposals across meetings.

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PRIMARY KEY | Tracked item ID |
| `tenant_id` | TEXT NOT NULL | Tenant tracking this item (FK to tenants) |
| `item_type` | TEXT NOT NULL | Type: 'ordinance', 'proposal', 'resolution' |
| `title` | TEXT NOT NULL | Item title |
| `description` | TEXT | Description |
| `banana` | TEXT NOT NULL | City identifier (FK to cities) |
| `first_mentioned_meeting_id` | TEXT | First meeting where mentioned (FK to meetings) |
| `first_seen` | TIMESTAMP | First mention timestamp |
| `last_seen` | TIMESTAMP | Most recent mention timestamp |
| `status` | TEXT | Status: 'active', 'passed', 'rejected' |
| `metadata` | JSONB | Additional metadata |

**Constraints:**
- `FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE`
- `FOREIGN KEY (banana) REFERENCES cities(banana) ON DELETE CASCADE`
- `FOREIGN KEY (first_mentioned_meeting_id) REFERENCES meetings(id) ON DELETE SET NULL`

---

### `tracked_item_meetings`

Links tracked items to meetings where mentioned.

| Column | Type | Description |
|--------|------|-------------|
| `tracked_item_id` | TEXT NOT NULL | Tracked item ID (FK to tracked_items) |
| `meeting_id` | TEXT NOT NULL | Meeting ID (FK to meetings) |
| `mentioned_at` | TIMESTAMP | When this link was created |
| `excerpt` | TEXT | Relevant excerpt from meeting |

**Primary Key:** Composite `(tracked_item_id, meeting_id)`

**Constraints:**
- `FOREIGN KEY (tracked_item_id) REFERENCES tracked_items(id) ON DELETE CASCADE`
- `FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE`

---

## JSON Structures

### `topics` (canonical topic identifiers)

```json
["housing", "zoning", "transportation"]
```

**Canonical Topics:**
- `housing` - Housing & Development
- `zoning` - Zoning & Land Use
- `transportation` - Transportation & Traffic
- `budget` - Budget & Finance
- `public_safety` - Public Safety
- `environment` - Environment & Sustainability
- `parks` - Parks & Recreation
- `utilities` - Utilities & Infrastructure
- `economic_development` - Economic Development
- `education` - Education & Schools
- `health` - Public Health
- `planning` - City Planning
- `permits` - Permits & Licensing
- `contracts` - Contracts & Procurement
- `appointments` - Appointments & Personnel
- `other` - Other

---

### `participation` (meetings table)

```json
{
  "email": "council@cityofpaloalto.org",
  "phone": "+1-650-329-2477",
  "virtual_url": "https://zoom.us/j/123456789",
  "meeting_id": "123456789",
  "is_hybrid": true
}
```

---

### `attachments` (items and city_matters tables)

```json
[
  {
    "name": "Staff Report",
    "url": "https://granicus.com/MetaViewer.php?meta_id=845318",
    "type": "pdf",
    "meta_id": "845318"
  }
]
```

---

### `vote_tally` (matter_appearances table)

```json
{
  "yes": 5,
  "no": 2,
  "abstain": 1,
  "absent": 1
}
```

---

## Indices

### Performance Indices

```sql
-- Cities
CREATE INDEX idx_cities_vendor ON cities(vendor);
CREATE INDEX idx_cities_state ON cities(state);
CREATE INDEX idx_cities_status ON cities(status);
CREATE INDEX idx_cities_population ON cities(population DESC NULLS LAST);
CREATE INDEX idx_cities_geom ON cities USING GIST (geom);

-- Meetings
CREATE INDEX idx_meetings_banana ON meetings(banana);
CREATE INDEX idx_meetings_date ON meetings(date);
CREATE INDEX idx_meetings_status ON meetings(processing_status);
CREATE INDEX idx_meetings_committee ON meetings(committee_id);
CREATE INDEX idx_meetings_banana_date ON meetings(banana, date DESC);

-- Items
CREATE INDEX idx_items_meeting_id ON items(meeting_id);
CREATE INDEX idx_items_matter_id ON items(matter_id);
CREATE INDEX idx_items_matter_file ON items(matter_file);
CREATE INDEX idx_items_meeting_sequence ON items(meeting_id, sequence ASC);

-- Matters
CREATE INDEX idx_city_matters_banana ON city_matters(banana);
CREATE INDEX idx_city_matters_status ON city_matters(status);
CREATE INDEX idx_city_matters_matter_file ON city_matters(matter_file);
CREATE INDEX idx_city_matters_first_seen ON city_matters(first_seen);
CREATE INDEX idx_city_matters_banana_last_seen ON city_matters(banana, last_seen DESC);

-- Council Members
CREATE INDEX idx_council_members_banana ON council_members(banana);
CREATE INDEX idx_council_members_status ON council_members(status);
CREATE INDEX idx_council_members_normalized ON council_members(normalized_name);
CREATE INDEX idx_council_members_banana_status ON council_members(banana, status);

-- Committees
CREATE INDEX idx_committees_banana ON committees(banana);
CREATE INDEX idx_committees_status ON committees(status);
CREATE INDEX idx_committees_name ON committees(normalized_name);

-- Votes
CREATE INDEX idx_votes_member ON votes(council_member_id);
CREATE INDEX idx_votes_matter ON votes(matter_id);
CREATE INDEX idx_votes_meeting ON votes(meeting_id);
CREATE INDEX idx_votes_value ON votes(vote);
CREATE INDEX idx_votes_member_date ON votes(council_member_id, vote_date DESC);

-- Sponsorships
CREATE INDEX idx_sponsorships_member ON sponsorships(council_member_id);
CREATE INDEX idx_sponsorships_matter ON sponsorships(matter_id);

-- Topics (junction tables)
CREATE INDEX idx_meeting_topics_topic ON meeting_topics(topic);
CREATE INDEX idx_item_topics_topic ON item_topics(topic);
CREATE INDEX idx_matter_topics_topic ON matter_topics(topic);

-- Queue
CREATE INDEX idx_queue_status ON queue(status);
CREATE INDEX idx_queue_city ON queue(banana);
CREATE INDEX idx_queue_processing ON queue(status, priority DESC, created_at ASC);
```

### Covering Indices (FTS Optimization)

```sql
-- Meetings by city sorted by date (includes id, title, summary for covering)
CREATE INDEX idx_meetings_banana_date_covering
    ON meetings(banana, date DESC) INCLUDE (id, title, summary);

-- Items by meeting sorted by sequence
CREATE INDEX idx_items_meeting_sequence
    ON items(meeting_id, sequence ASC);

-- Matters by city for listing
CREATE INDEX idx_city_matters_banana_last_seen
    ON city_matters(banana, last_seen DESC);
```

### Full-Text Search Indices

```sql
-- Stored tsvector columns (5-10x faster than expression indexes)
CREATE INDEX idx_meetings_search_vector ON meetings USING gin(search_vector);
CREATE INDEX idx_items_search_vector ON items USING gin(search_vector);
CREATE INDEX idx_city_matters_search_vector ON city_matters USING gin(search_vector);

-- Full-text search on names
CREATE INDEX idx_council_members_fts ON council_members USING gin(to_tsvector('english', name));
CREATE INDEX idx_committees_fts ON committees USING gin(to_tsvector('english', name));
```

---

## Relationships

### Entity Relationship Diagram

```
                                cities
                                  |
          +----------+------------+------------+-----------+
          |          |            |            |           |
          v          v            v            v           v
      zipcodes   meetings    city_matters  committees  council_members
                    |            |            |           |
                    v            |            v           |
                  items <--------+     committee_members<-+
                    |            |                        |
                    +-----+      v                        v
                    |     |  matter_appearances        votes
                    v     |      |                        |
              item_topics |      +------------------------+
                          |                |
                          v                v
                   happening_items   sponsorships


                            userland.users
                                  |
          +----------+------------+------------+-----------+
          |          |            |            |           |
          v          v            v            v           v
       alerts    watches     activity_log  ratings     issues
          |
          v
    alert_matches

                             deliberations
                                  |
          +----------+------------+------------+
          |          |            |
          v          v            v
    participants  comments     results
                      |
                      v
                    votes
```

### Key Relationships

**cities -> meetings** (1:N)
- One city has many meetings
- Cascade delete

**cities -> city_matters** (1:N)
- One city has many matters
- Cascade delete

**cities -> council_members** (1:N)
- One city has many council members
- Cascade delete

**cities -> committees** (1:N)
- One city has many committees
- Cascade delete

**meetings -> items** (1:N)
- One meeting has many agenda items
- Cascade delete

**city_matters -> items** (1:N)
- One matter can appear in many items
- SET NULL on delete

**city_matters -> matter_appearances** (1:N)
- One matter has many appearances
- Cascade delete

**council_members -> votes** (1:N)
- One member casts many votes
- Cascade delete

**council_members -> sponsorships** (1:N)
- One member sponsors many matters
- Cascade delete

**committees -> committee_members** (1:N)
- One committee has many member assignments
- Cascade delete

**userland.users -> userland.alerts** (1:N)
- One user has many alerts
- Cascade delete

**userland.users -> userland.watches** (1:N)
- One user has many watched entities
- Cascade delete

**userland.alerts -> userland.alert_matches** (1:N)
- One alert has many matches
- Cascade delete

**deliberations -> deliberation_participants** (1:N)
- One deliberation has many participants
- Cascade delete

**deliberations -> deliberation_comments** (1:N)
- One deliberation has many comments
- Cascade delete

---

## Querying Examples

### Get all votes by a council member
```sql
SELECT v.*, cm.title as matter_title, m.date as vote_date
FROM votes v
JOIN city_matters cm ON v.matter_id = cm.id
JOIN meetings m ON v.meeting_id = m.id
WHERE v.council_member_id = 'nashvilleTN_abc123'
ORDER BY m.date DESC
LIMIT 50;
```

### Get committee roster
```sql
SELECT c.name as member_name, c.title, cm.role
FROM committee_members cm
JOIN council_members c ON cm.council_member_id = c.id
WHERE cm.committee_id = 'sanfranciscoCA_planning'
  AND cm.left_at IS NULL
ORDER BY cm.role DESC, c.name;
```

### Get matter voting history
```sql
SELECT
  m.date,
  ma.committee,
  ma.vote_outcome,
  ma.vote_tally
FROM matter_appearances ma
JOIN meetings m ON ma.meeting_id = m.id
WHERE ma.matter_id = 'nashvilleTN_BL2025-001'
ORDER BY ma.appeared_at;
```

### Find meetings by topic
```sql
SELECT m.*
FROM meetings m
JOIN meeting_topics mt ON m.id = mt.meeting_id
WHERE mt.topic = 'housing'
ORDER BY m.date DESC
LIMIT 20;
```

---

## Database Maintenance

### Backup (PostgreSQL)

```bash
# Daily backup
pg_dump -U engagic -d engagic > /root/backups/engagic_$(date +%Y%m%d).sql

# Restore
psql -U engagic -d engagic < /root/backups/engagic_20251201.sql
```

### Statistics

```sql
-- Core table row counts
SELECT
  'cities' as table_name, COUNT(*) as rows FROM cities
UNION ALL SELECT 'meetings', COUNT(*) FROM meetings
UNION ALL SELECT 'items', COUNT(*) FROM items
UNION ALL SELECT 'city_matters', COUNT(*) FROM city_matters
UNION ALL SELECT 'council_members', COUNT(*) FROM council_members
UNION ALL SELECT 'committees', COUNT(*) FROM committees
UNION ALL SELECT 'votes', COUNT(*) FROM votes
UNION ALL SELECT 'sponsorships', COUNT(*) FROM sponsorships
UNION ALL SELECT 'deliberations', COUNT(*) FROM deliberations
ORDER BY table_name;

-- Userland table row counts
SELECT
  'users' as table_name, COUNT(*) as rows FROM userland.users
UNION ALL SELECT 'alerts', COUNT(*) FROM userland.alerts
UNION ALL SELECT 'watches', COUNT(*) FROM userland.watches
UNION ALL SELECT 'ratings', COUNT(*) FROM userland.ratings
ORDER BY table_name;
```

---

**Last Updated:** December 16, 2025

**See Also:** [../database/README.md](../database/README.md) for repository pattern and Python code usage
