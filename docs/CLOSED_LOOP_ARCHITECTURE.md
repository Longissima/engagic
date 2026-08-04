# Closed Loop Architecture

> **STATUS (2026-08-04): Gap 1's core wiring has since shipped.** Votes persist via
> `pipeline/orchestrators/meeting_sync.py:611-619` → `council_members.record_votes_for_matter`;
> the live DB holds ~298K per-member vote rows across ~2,900 members. `vote_tally` /
> `vote_outcome` populate wherever vendors expose votes (Legistar + Chicago, ~13% of
> jurisdictions). The rest of this doc's Gap 1 framing describes the pre-wiring state;
> Gaps 2-3 remain open. The remaining outcome gap is *coverage*, not wiring — minutes
> parsing for the other ~87% is tracked in spygov's `docs/DATA_SPINE.md` (Known gaps).

> The system answers "what's being discussed" but never "what happened."

---

## The Problem

Engagic is a one-way information pipeline:

```
Government Data --> Processing --> Display --> ???
```

We've built an exceptional **awareness engine** - 374+ cities, item-level processing, matter deduplication, topic extraction. But we have no **outcome engine**. A user can watch a matter through five meetings of discussion and never learn: "It passed 4-3 on November 15th."

This blocks everything downstream:
- Can't prove civic engagement works
- Can't create success stories ("100 residents testified, ordinance modified")
- Can't build retention loops ("come back to see how your issue resolved")
- Can't enable viral growth ("share because it passed!")

---

## The Discovery

Analysis of the codebase revealed something unexpected: **the infrastructure for outcome tracking is 90% built**.

| Component | Status | Location |
|-----------|--------|----------|
| `votes` table | Complete | `database/schema_postgres.sql` |
| `council_members` table | Complete | `database/schema_postgres.sql` |
| Vote recording methods | Complete | `database/repositories_async/council_members.py:424-568` |
| Vote tally computation | Complete | `database/repositories_async/council_members.py:689` |
| Legistar vote extraction | Complete | `vendors/adapters/legistar_adapter_async.py:463-534` |
| `matter_appearances.vote_tally` | Column exists | Always NULL |
| `matter_appearances.vote_outcome` | Column exists | Always NULL |
| `city_matters.status` | Column exists | Always "active" |

**The gap**: Votes are extracted from the Legistar API into `item["votes"]`, then discarded. The wiring from extraction to persistence is missing.

---

## Three Gaps to Close

### Gap 1: Outcome Tracking
Close the loop from "item discussed" to "item resolved."

### Gap 2: Engagement Mechanics
Create user action loops and social proof.

### Gap 3: User Feedback Loop
Let humans teach the system what's valuable.

These form a virtuous cycle:
- Outcomes make watches valuable ("matter you're following passed 7-2")
- Engagement generates rating opportunities
- Feedback improves quality, driving more engagement

---

## Gap 1: Outcome Tracking

### Current State

```python
# In legistar_adapter_async.py:463
votes = await self._fetch_event_item_votes_api(event_id, item_id)
item["votes"] = votes  # Extracted, attached to item

# In processor.py
# ... votes are never read, never persisted
```

### Schema Changes

```sql
-- Expand status enum (currently only 'active')
ALTER TABLE city_matters
  DROP CONSTRAINT IF EXISTS city_matters_status_check,
  ADD CONSTRAINT city_matters_status_check CHECK (
    status IN ('active', 'passed', 'failed', 'tabled',
               'withdrawn', 'referred', 'amended', 'vetoed', 'enacted')
  );

-- Track when matter reached final disposition
ALTER TABLE city_matters ADD COLUMN final_vote_date TIMESTAMP;
```

### Pipeline Integration

Add to `pipeline/processor.py` after item summarization:

```python
async def persist_votes(self, meeting: Meeting, item: Item) -> None:
    """Wire vote extraction to database persistence."""
    if not item.votes:
        return

    # Record individual votes (roll call)
    await self.db.council_members.record_votes_for_matter(
        banana=meeting.banana,
        matter_id=item.matter_id,
        meeting_id=meeting.id,
        votes=item.votes,
        vote_date=meeting.date
    )

    # Compute aggregate tally
    tally = self._compute_vote_tally(item.votes)
    outcome = self._determine_outcome(tally)

    # Update matter appearance with outcome
    await self.db.matters.update_appearance_outcome(
        matter_id=item.matter_id,
        meeting_id=meeting.id,
        vote_outcome=outcome,
        vote_tally=tally
    )

    # If this looks like a final vote, update matter status
    if outcome in ('passed', 'failed') and self._is_final_reading(item):
        await self.db.matters.update_status(
            matter_id=item.matter_id,
            status=outcome,
            final_vote_date=meeting.date
        )

def _compute_vote_tally(self, votes: list[dict]) -> dict:
    """Aggregate individual votes into tally."""
    tally = {'yes': 0, 'no': 0, 'abstain': 0, 'absent': 0}
    for vote in votes:
        key = vote.get('vote', 'absent').lower()
        if key in tally:
            tally[key] += 1
    return tally

def _determine_outcome(self, tally: dict) -> str:
    """Determine pass/fail from tally."""
    if tally['yes'] > tally['no']:
        return 'passed'
    elif tally['no'] > tally['yes']:
        return 'failed'
    else:
        return 'tabled'  # Tie or no votes
```

### Repository Addition

Add to `database/repositories_async/matters.py`:

```python
async def update_appearance_outcome(
    self,
    matter_id: str,
    meeting_id: str,
    vote_outcome: str,
    vote_tally: dict
) -> None:
    """Record vote outcome on matter appearance."""
    await self.execute(
        """
        UPDATE matter_appearances
        SET vote_outcome = $1, vote_tally = $2
        WHERE matter_id = $3 AND meeting_id = $4
        """,
        vote_outcome, json.dumps(vote_tally), matter_id, meeting_id
    )

async def update_status(
    self,
    matter_id: str,
    status: str,
    final_vote_date: datetime
) -> None:
    """Update matter disposition status."""
    await self.execute(
        """
        UPDATE city_matters
        SET status = $1, final_vote_date = $2
        WHERE id = $3
        """,
        status, final_vote_date, matter_id
    )
```

### API Endpoints

New file: `server/routes/votes.py`

```python
@router.get("/api/matters/{matter_id}/votes")
async def get_matter_votes(matter_id: str, db: DB = Depends(get_db)):
    """All votes on a matter across all meetings."""
    votes = await db.council_members.get_votes_for_matter(matter_id)
    tally = await db.council_members.get_vote_tally_for_matter(matter_id)
    return {"votes": votes, "tally": tally}

@router.get("/api/meetings/{meeting_id}/votes")
async def get_meeting_votes(meeting_id: str, db: DB = Depends(get_db)):
    """All votes cast in a meeting."""
    return await db.council_members.get_votes_for_meeting(meeting_id)

@router.get("/api/council-members/{member_id}/votes")
async def get_member_votes(member_id: str, db: DB = Depends(get_db)):
    """Voting record for a council member."""
    return await db.council_members.get_member_voting_record(member_id)

@router.get("/api/city/{banana}/council-members")
async def get_city_council(banana: str, db: DB = Depends(get_db)):
    """City council roster with vote counts."""
    return await db.council_members.get_city_roster(banana)
```

### Backfill Strategy

Historical votes exist in Legistar API. Backfill script:

```python
async def backfill_votes():
    """Re-fetch votes for all Legistar cities."""
    legistar_cities = await db.cities.get_by_vendor('legistar')
    for city in legistar_cities:
        meetings = await db.meetings.get_processed(city.banana)
        for meeting in meetings:
            adapter = LegistarAdapter(city)
            items = await adapter.fetch_items_with_votes(meeting.vendor_id)
            for item in items:
                if item.votes:
                    await persist_votes(meeting, item)
```

Estimated scope: ~20 Legistar cities, ~50k historical votes.

---

## Gap 2: Engagement Mechanics

### Current State

- No way to mark items as "watching"
- No social proof ("X people following this")
- No trending based on user interest
- Alerts are the only re-engagement mechanism
- Every user is isolated

### Schema

Add to `userland` schema:

```sql
-- User watches (matters, meetings, topics, cities, council members)
CREATE TABLE userland.watches (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES userland.users(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL CHECK (
        entity_type IN ('matter', 'meeting', 'topic', 'city', 'council_member')
    ),
    entity_id TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(user_id, entity_type, entity_id)
);

CREATE INDEX watches_entity_idx ON userland.watches(entity_type, entity_id);
CREATE INDEX watches_user_idx ON userland.watches(user_id);

-- Activity log (views, watches, searches, shares)
CREATE TABLE userland.activity_log (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT,                    -- NULL for anonymous
    session_id TEXT,                 -- For anonymous tracking
    action TEXT NOT NULL CHECK (
        action IN ('view', 'watch', 'unwatch', 'search', 'share', 'rate', 'report')
    ),
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    metadata JSONB,                  -- Search query, referrer, etc.
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX activity_log_entity_idx ON userland.activity_log(entity_type, entity_id);
CREATE INDEX activity_log_time_idx ON userland.activity_log(created_at DESC);

-- Trending matters (materialized, refresh every 15 min)
CREATE MATERIALIZED VIEW userland.trending_matters AS
SELECT
    entity_id AS matter_id,
    COUNT(*) AS engagement,
    COUNT(DISTINCT COALESCE(user_id, session_id)) AS unique_users
FROM userland.activity_log
WHERE entity_type = 'matter'
  AND created_at > NOW() - INTERVAL '7 days'
GROUP BY entity_id
ORDER BY engagement DESC
LIMIT 100;

CREATE UNIQUE INDEX trending_matters_idx ON userland.trending_matters(matter_id);
```

### Repository

New file: `database/repositories_async/engagement.py`

```python
@dataclass
class Watch:
    id: int
    user_id: str
    entity_type: str
    entity_id: str
    created_at: datetime

@dataclass
class TrendingMatter:
    matter_id: str
    engagement: int
    unique_users: int

class EngagementRepository(BaseRepository):

    async def watch(self, user_id: str, entity_type: str, entity_id: str) -> None:
        """Add entity to user's watch list."""
        await self.execute(
            """
            INSERT INTO userland.watches (user_id, entity_type, entity_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, entity_type, entity_id) DO NOTHING
            """,
            user_id, entity_type, entity_id
        )
        await self.log_activity(user_id, None, 'watch', entity_type, entity_id)

    async def unwatch(self, user_id: str, entity_type: str, entity_id: str) -> None:
        """Remove entity from user's watch list."""
        await self.execute(
            """
            DELETE FROM userland.watches
            WHERE user_id = $1 AND entity_type = $2 AND entity_id = $3
            """,
            user_id, entity_type, entity_id
        )
        await self.log_activity(user_id, None, 'unwatch', entity_type, entity_id)

    async def get_watch_count(self, entity_type: str, entity_id: str) -> int:
        """Count users watching an entity."""
        row = await self.fetchrow(
            """
            SELECT COUNT(*) FROM userland.watches
            WHERE entity_type = $1 AND entity_id = $2
            """,
            entity_type, entity_id
        )
        return row['count']

    async def is_watching(self, user_id: str, entity_type: str, entity_id: str) -> bool:
        """Check if user is watching an entity."""
        row = await self.fetchrow(
            """
            SELECT 1 FROM userland.watches
            WHERE user_id = $1 AND entity_type = $2 AND entity_id = $3
            """,
            user_id, entity_type, entity_id
        )
        return row is not None

    async def get_user_watches(self, user_id: str) -> list[Watch]:
        """Get all entities a user is watching."""
        rows = await self.fetch(
            """
            SELECT * FROM userland.watches
            WHERE user_id = $1
            ORDER BY created_at DESC
            """,
            user_id
        )
        return [Watch(**row) for row in rows]

    async def log_activity(
        self,
        user_id: str | None,
        session_id: str | None,
        action: str,
        entity_type: str,
        entity_id: str | None = None,
        metadata: dict | None = None
    ) -> None:
        """Record user activity for analytics and trending."""
        await self.execute(
            """
            INSERT INTO userland.activity_log
                (user_id, session_id, action, entity_type, entity_id, metadata)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            user_id, session_id, action, entity_type, entity_id,
            json.dumps(metadata) if metadata else None
        )

    async def get_trending_matters(self, limit: int = 20) -> list[TrendingMatter]:
        """Get trending matters from materialized view."""
        rows = await self.fetch(
            "SELECT * FROM userland.trending_matters LIMIT $1",
            limit
        )
        return [TrendingMatter(**row) for row in rows]

    async def refresh_trending(self) -> None:
        """Refresh trending materialized view."""
        await self.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY userland.trending_matters")
```

### API Endpoints

New file: `server/routes/engagement.py`

```python
@router.post("/api/watch/{entity_type}/{entity_id}")
async def watch_entity(
    entity_type: str,
    entity_id: str,
    user: User = Depends(get_current_user),
    db: DB = Depends(get_db)
):
    """Add entity to watch list."""
    await db.engagement.watch(user.id, entity_type, entity_id)
    return {"status": "watching"}

@router.delete("/api/watch/{entity_type}/{entity_id}")
async def unwatch_entity(
    entity_type: str,
    entity_id: str,
    user: User = Depends(get_current_user),
    db: DB = Depends(get_db)
):
    """Remove entity from watch list."""
    await db.engagement.unwatch(user.id, entity_type, entity_id)
    return {"status": "unwatched"}

@router.get("/api/me/watching")
async def get_user_watches(
    user: User = Depends(get_current_user),
    db: DB = Depends(get_db)
):
    """Get user's watched entities."""
    watches = await db.engagement.get_user_watches(user.id)
    return {"watches": watches}

@router.get("/api/trending/matters")
async def get_trending_matters(
    limit: int = 20,
    db: DB = Depends(get_db)
):
    """Get trending matters."""
    matters = await db.engagement.get_trending_matters(limit)
    return {"trending": matters}

@router.get("/api/matters/{matter_id}/engagement")
async def get_matter_engagement(
    matter_id: str,
    user: User | None = Depends(get_optional_user),
    db: DB = Depends(get_db)
):
    """Get engagement stats for a matter."""
    watch_count = await db.engagement.get_watch_count('matter', matter_id)
    is_watching = False
    if user:
        is_watching = await db.engagement.is_watching(user.id, 'matter', matter_id)
    return {
        "watch_count": watch_count,
        "is_watching": is_watching
    }
```

### Activity Logging Middleware

Add to `server/middleware.py`:

```python
async def activity_logging_middleware(request: Request, call_next):
    """Log page views for analytics."""
    response = await call_next(request)

    # Log views for entity pages
    path = request.url.path
    if match := re.match(r'/api/(matters|meetings|items)/([^/]+)$', path):
        entity_type, entity_id = match.groups()
        user_id = getattr(request.state, 'user_id', None)
        session_id = request.cookies.get('session_id')

        await request.state.db.engagement.log_activity(
            user_id, session_id, 'view', entity_type, entity_id
        )

    return response
```

### Trending Refresh

Add to systemd timers or conductor:

```python
# In conductor.py or separate timer
async def refresh_trending_task():
    """Refresh trending view every 15 minutes."""
    while True:
        await db.engagement.refresh_trending()
        await asyncio.sleep(900)  # 15 minutes
```

---

## Gap 3: User Feedback Loop

### Current State

- `scripts/summary_quality_checker.py` detects bad summaries offline
- No way for users to report issues
- No ratings, no thumbs up/down
- System can't learn what users find valuable

### Schema

Add to `userland` schema:

```sql
-- Summary ratings (1-5 stars)
CREATE TABLE userland.ratings (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT,                    -- NULL for anonymous
    session_id TEXT,                 -- For anonymous rating
    entity_type TEXT NOT NULL CHECK (entity_type IN ('item', 'meeting', 'matter')),
    entity_id TEXT NOT NULL,
    rating SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    created_at TIMESTAMP DEFAULT NOW(),
    -- One rating per user/session per entity
    UNIQUE(COALESCE(user_id, session_id), entity_type, entity_id)
);

CREATE INDEX ratings_entity_idx ON userland.ratings(entity_type, entity_id);

-- Issue reports
CREATE TABLE userland.issues (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT,
    session_id TEXT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    issue_type TEXT NOT NULL CHECK (
        issue_type IN ('inaccurate', 'incomplete', 'misleading', 'offensive', 'other')
    ),
    description TEXT NOT NULL,
    status TEXT DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'dismissed')),
    admin_notes TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    resolved_at TIMESTAMP
);

CREATE INDEX issues_status_idx ON userland.issues(status);
CREATE INDEX issues_entity_idx ON userland.issues(entity_type, entity_id);

-- Denormalized quality scores (updated via trigger or application)
ALTER TABLE items ADD COLUMN IF NOT EXISTS quality_score REAL;
ALTER TABLE items ADD COLUMN IF NOT EXISTS rating_count INTEGER DEFAULT 0;
ALTER TABLE city_matters ADD COLUMN IF NOT EXISTS quality_score REAL;
ALTER TABLE city_matters ADD COLUMN IF NOT EXISTS rating_count INTEGER DEFAULT 0;
```

### Repository

New file: `database/repositories_async/feedback.py`

```python
@dataclass
class RatingStats:
    avg_rating: float
    rating_count: int
    distribution: dict[int, int]  # {1: 5, 2: 3, 3: 10, ...}

@dataclass
class Issue:
    id: int
    user_id: str | None
    entity_type: str
    entity_id: str
    issue_type: str
    description: str
    status: str
    created_at: datetime

class FeedbackRepository(BaseRepository):

    async def submit_rating(
        self,
        user_id: str | None,
        session_id: str | None,
        entity_type: str,
        entity_id: str,
        rating: int
    ) -> None:
        """Submit or update a rating."""
        # Upsert rating
        await self.execute(
            """
            INSERT INTO userland.ratings (user_id, session_id, entity_type, entity_id, rating)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (COALESCE(user_id, session_id), entity_type, entity_id)
            DO UPDATE SET rating = $5
            """,
            user_id, session_id, entity_type, entity_id, rating
        )

        # Update denormalized quality score
        await self._update_quality_score(entity_type, entity_id)

    async def _update_quality_score(self, entity_type: str, entity_id: str) -> None:
        """Recalculate quality score from ratings."""
        stats = await self.get_entity_rating(entity_type, entity_id)

        table = 'items' if entity_type == 'item' else 'city_matters'
        await self.execute(
            f"""
            UPDATE {table}
            SET quality_score = $1, rating_count = $2
            WHERE id = $3
            """,
            stats.avg_rating, stats.rating_count, entity_id
        )

    async def report_issue(
        self,
        user_id: str | None,
        session_id: str | None,
        entity_type: str,
        entity_id: str,
        issue_type: str,
        description: str
    ) -> int:
        """Report an issue with a summary."""
        row = await self.fetchrow(
            """
            INSERT INTO userland.issues
                (user_id, session_id, entity_type, entity_id, issue_type, description)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            user_id, session_id, entity_type, entity_id, issue_type, description
        )
        return row['id']

    async def get_entity_rating(self, entity_type: str, entity_id: str) -> RatingStats:
        """Get rating statistics for an entity."""
        rows = await self.fetch(
            """
            SELECT rating, COUNT(*) as count
            FROM userland.ratings
            WHERE entity_type = $1 AND entity_id = $2
            GROUP BY rating
            """,
            entity_type, entity_id
        )

        distribution = {i: 0 for i in range(1, 6)}
        total = 0
        weighted_sum = 0

        for row in rows:
            distribution[row['rating']] = row['count']
            total += row['count']
            weighted_sum += row['rating'] * row['count']

        avg = weighted_sum / total if total > 0 else 0
        return RatingStats(avg_rating=avg, rating_count=total, distribution=distribution)

    async def get_low_rated_entities(
        self,
        threshold: float = 2.5,
        min_ratings: int = 3
    ) -> list[tuple[str, str]]:
        """Get entities with low ratings for reprocessing."""
        rows = await self.fetch(
            """
            SELECT entity_type, entity_id
            FROM userland.ratings
            GROUP BY entity_type, entity_id
            HAVING AVG(rating) <= $1 AND COUNT(*) >= $2
            """,
            threshold, min_ratings
        )
        return [(row['entity_type'], row['entity_id']) for row in rows]

    async def get_open_issues(self, limit: int = 100) -> list[Issue]:
        """Get unresolved issues for admin review."""
        rows = await self.fetch(
            """
            SELECT * FROM userland.issues
            WHERE status = 'open'
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit
        )
        return [Issue(**row) for row in rows]

    async def resolve_issue(self, issue_id: int, status: str, admin_notes: str = None) -> None:
        """Mark issue as resolved or dismissed."""
        await self.execute(
            """
            UPDATE userland.issues
            SET status = $1, admin_notes = $2, resolved_at = NOW()
            WHERE id = $3
            """,
            status, admin_notes, issue_id
        )
```

### API Endpoints

New file: `server/routes/feedback.py`

```python
@router.post("/api/rate/{entity_type}/{entity_id}")
async def rate_entity(
    entity_type: str,
    entity_id: str,
    rating: int = Body(..., ge=1, le=5),
    user: User | None = Depends(get_optional_user),
    session_id: str = Cookie(None),
    db: DB = Depends(get_db)
):
    """Submit rating for an entity."""
    if not user and not session_id:
        raise HTTPException(400, "Authentication or session required")

    await db.feedback.submit_rating(
        user.id if user else None,
        session_id,
        entity_type,
        entity_id,
        rating
    )
    return {"status": "rated"}

@router.post("/api/report/{entity_type}/{entity_id}")
async def report_issue(
    entity_type: str,
    entity_id: str,
    issue_type: str = Body(...),
    description: str = Body(...),
    user: User | None = Depends(get_optional_user),
    session_id: str = Cookie(None),
    db: DB = Depends(get_db)
):
    """Report an issue with a summary."""
    if not user and not session_id:
        raise HTTPException(400, "Authentication or session required")

    issue_id = await db.feedback.report_issue(
        user.id if user else None,
        session_id,
        entity_type,
        entity_id,
        issue_type,
        description
    )
    return {"status": "reported", "issue_id": issue_id}

@router.get("/api/{entity_type}/{entity_id}/rating")
async def get_entity_rating(
    entity_type: str,
    entity_id: str,
    db: DB = Depends(get_db)
):
    """Get rating statistics for an entity."""
    stats = await db.feedback.get_entity_rating(entity_type, entity_id)
    return stats
```

### Reprocessing Integration

Add to conductor or separate script:

```python
async def reprocess_low_rated():
    """Queue low-rated items for reprocessing."""
    low_rated = await db.feedback.get_low_rated_entities()

    for entity_type, entity_id in low_rated:
        if entity_type == 'item':
            await db.queue.enqueue(
                item_id=entity_id,
                priority=10,  # High priority
                reason='low_user_rating'
            )
            logger.info(f"Queued {entity_id} for reprocessing (low rating)")
```

---

## Implementation Order

### Phase 1: Outcome Tracking

| Step | File | Change |
|------|------|--------|
| 1 | `database/schema_postgres.sql` | Expand status enum, add final_vote_date |
| 2 | `database/repositories_async/matters.py` | Add `update_appearance_outcome()`, `update_status()` |
| 3 | `pipeline/processor.py` | Wire vote persistence after summarization |
| 4 | `server/routes/votes.py` | New file with vote endpoints |
| 5 | `server/main.py` | Register votes router |
| 6 | `scripts/backfill_votes.py` | Historical vote backfill |

### Phase 2: Engagement Mechanics

| Step | File | Change |
|------|------|--------|
| 1 | `database/schema_postgres.sql` | Add watches, activity_log, trending_matters |
| 2 | `database/repositories_async/engagement.py` | New repository |
| 3 | `database/db_postgres.py` | Wire engagement repository |
| 4 | `server/routes/engagement.py` | New file with watch/trending endpoints |
| 5 | `server/middleware.py` | Activity logging middleware |
| 6 | `server/main.py` | Register engagement router |
| 7 | Systemd timer | Trending refresh every 15 min |

### Phase 3: User Feedback

| Step | File | Change |
|------|------|--------|
| 1 | `database/schema_postgres.sql` | Add ratings, issues tables |
| 2 | `database/repositories_async/feedback.py` | New repository |
| 3 | `database/db_postgres.py` | Wire feedback repository |
| 4 | `server/routes/feedback.py` | New file with rating endpoints |
| 5 | `server/main.py` | Register feedback router |
| 6 | `pipeline/conductor.py` | Low-rated reprocessing task |

---

## Critical Files Summary

**Gap 1 (Outcome Tracking):**
- `pipeline/processor.py` - Vote persistence entry point
- `database/repositories_async/council_members.py:424-731` - Vote methods (exist)
- `database/repositories_async/matters.py` - Add outcome methods
- `vendors/adapters/legistar_adapter_async.py:463-534` - Vote extraction (exists)

**Gap 2 (Engagement):**
- `database/repositories_async/engagement.py` - New
- `server/routes/engagement.py` - New
- `server/middleware.py` - Activity logging

**Gap 3 (Feedback):**
- `database/repositories_async/feedback.py` - New
- `server/routes/feedback.py` - New
- `pipeline/conductor.py` - Reprocessing integration

---

## The Closed Loop

```
                    +------------------+
                    |  Government Data |
                    +--------+---------+
                             |
                             v
                    +--------+---------+
                    |  Vendor Adapter  |
                    |  (extract votes) |
                    +--------+---------+
                             |
                             v
                    +--------+---------+
                    |    Processor     |
                    | (persist votes)  |  <-- GAP 1
                    +--------+---------+
                             |
                             v
                    +--------+---------+
                    |    Database      |
                    | (outcomes stored)|
                    +--------+---------+
                             |
                             v
                    +--------+---------+
                    |       API        |
                    | (serve outcomes) |
                    +--------+---------+
                             |
                             v
                    +--------+---------+
                    |     Frontend     |
                    | (display + watch)|
                    +--------+---------+
                             |
              +--------------+--------------+
              |                             |
              v                             v
     +--------+--------+           +--------+--------+
     |   User Watches  |           |   User Rates    |
     |    (GAP 2)      |           |    (GAP 3)      |
     +--------+--------+           +--------+--------+
              |                             |
              v                             v
     +--------+--------+           +--------+--------+
     | Social Proof    |           | Quality Signal  |
     | Trending        |           | Reprocessing    |
     +-----------------+           +-----------------+
              |                             |
              +-------------+---------------+
                            |
                            v
                   +--------+--------+
                   |  Network Effect |
                   |     Growth      |
                   +-----------------+
```

---

*Last updated: 2025-12-01*
