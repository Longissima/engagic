# Engagic Vision

**Last Updated:** December 1, 2025

---

## Inspiration

Local government is the most impactful layer of democracy - zoning, housing, schools, police budgets - yet the least accessible.

**The problem:**
- 250-page meeting packets released days before decisions
- Obscure vendor portals across 500+ platforms
- No way to know when issues you care about are being discussed
- Civic engagement becomes a privilege of those with time and expertise

**The opportunity:**
- AI can read and summarize these documents
- Scraping can unify fragmented data sources
- Topic extraction can match issues to citizens
- Open source can build trust in civic infrastructure

**The mission:** Make local government meetings accessible to everyone, not just political insiders.

---

## Intuition (Architecture)

Engagic is built in layers, each serving different users:

### Core Data Layer (Free OSS)
**What:** Scraping, processing, storage - the source of truth
**Users:** Civic hackers, researchers, other civic tech projects
**Access:** Direct database, open source codebase
**Principle:** Public data should be freely accessible and machine-readable

Components (6 logical clusters):
- **vendors/** - Adapters for 6 platforms (BaseAdapter pattern, 94% success rate)
- **parsing/** - PDF text extraction (PyMuPDF) and participation info
- **analysis/** - LLM intelligence (Gemini) and topic normalization (16 canonical topics)
- **pipeline/** - Processing orchestration (4 modules: conductor, fetcher, processor, analyzer)
- **database/** - PostgreSQL with Repository Pattern (cities, meetings, agenda_items, job_queue)
  - UnifiedDatabase facade delegates to 5 focused repositories
  - Clean separation: cities, meetings, items, queue, search operations
- **server/** - Modular FastAPI (main 98 lines, routes/services/utils in focused modules)

Architecture:
- Item-level processing for 374+ cities (58% of platform, 50-80M people)
- Two parallel pipelines: item-based (primary) and monolithic (fallback)
- Cache-first API (never fetches live, background fetcher service syncs every 24 hours)
- Priority queue (recent meetings first)

### Userland (Consumer Features)
**What:** Profiles, alerts, digests - built ON TOP of core data layer
**Users:** Civilians who want to engage with local government
**Access:** Web app, PWA with notifications
**Principle:** Design APIs that civilians can understand

Features (to build):
- User profiles: city + topics subscriptions
- Alert system: topic matching → notifications
- Weekly email digest: summaries of relevant meetings
- PWA notifications: iOS/Android push via service workers
- Mobile-first design

### Integrations (Third-Party Bridges)
**What:** Read-only bridges to decentralized platforms
**Users:** Communities building civic discourse tools
**Access:** Public API, read-only database views (NOT direct DB access)
**Principle:** Protect the source of truth, enable innovation

Examples:
- Bluesky: per-city boards/feeds
- ATProto: decentralized civic discourse
- Public API: filtered, rate-limited access

### Tenancy (B2B Paid Layer)
**What:** API keys, coverage filtering, webhooks
**Users:** Advocacy orgs, law firms, policy researchers
**Access:** Authenticated API with tenant-specific filtering
**Principle:** Sustainable funding enables free public tier

Features (to build):
- Tenant accounts with API keys
- Coverage filtering (only their cities)
- Keyword matching on summaries
- Usage analytics
- Webhooks (push notifications to client systems)

---

## Current State (December 2025)

> **Drift note (June 2026):** counts below are stale — 23 adapters now, plus
> the chunking cascade, extraction telemetry, ground-truth corpora, and the
> Gemini batch lane. See CHANGELOG 2026-06-11 and vendors/README for current
> reality.

**What Works:**
- ✅ 500+ cities, 374+ with item-level processing (58% coverage)
- ✅ Item-based frontend (navigable, scannable agendas, collapsible items)
- ✅ Topic extraction (16 canonical topics, frontend displays)
- ✅ Participation info (email/phone/Zoom, one-click civic action)
- ✅ Cache-first API (<100ms response times)
- ✅ Modular architecture (7 logical clusters, Repository Pattern, clean separation)
- ✅ Production-ready codebase (82% readiness): Zero linting errors, zero critical anti-patterns
- ✅ **User accounts/profiles** (magic link auth, city + keyword subscriptions)
- ✅ **Weekly digest emails** (Sundays 9am via Mailgun, keyword matching)
- ✅ **Council member + voting infrastructure** (schema, repos, Legistar extraction)

**In Progress:**
- Council member API endpoints and frontend pages
- Vote extraction for more adapters

**What's Missing:**
- ❌ Mobile PWA notifications
- ❌ Unsubscribe flow
- ❌ Campaign finance / donor tracking

**Current Users:** Civilians can now subscribe to cities and keywords and receive weekly digests.

See `CLAUDE.md` for detailed architecture and `CHANGELOG.md` for historical changes.

---

## Roadmap (Growth Features)

### Phase 0: Contact Info Parsing - ✅ COMPLETE
**Goal:** Make participation easier with contact info
**Status:** DEPLOYED (October 2025)
**Impact:** One-click civic action (mailto:, tel:, Zoom links)

### Phase 1: Topic Extraction - ✅ COMPLETE
**Goal:** Automatically tag meetings with topics
**Status:** DEPLOYED (October 2025)
**Impact:** Foundation for user subscriptions and smart filtering

### Phase 1.5: AI Thinking Traces - ✅ COMPLETE
**Goal:** Show how AI analyzed meetings (transparency)
**Status:** DEPLOYED (November 2025)
**Implementation:** Reactive Svelte templating, collapsible thinking sections
**Impact:** Users can verify AI reasoning, builds trust

### Phase 2: User Profiles (Core Growth) - COMPLETE
**Goal:** Let civilians subscribe to cities and topics
**Status:** DEPLOYED (November 2025)

**Implemented:**
- Magic link authentication (JWT, 15-min expiry)
- User profiles with city + keyword subscriptions
- Dashboard API and frontend integration
- PostgreSQL `userland` schema

### Phase 3: Alerts & Notifications - COMPLETE
**Goal:** Notify users when relevant meetings happen
**Status:** DEPLOYED (November 2025)

**Implemented:**
- Weekly digest emails (Sundays 9am via Mailgun)
- Keyword matching on item summaries
- Matter-based deduplication
- Keyword highlighting in emails

**Remaining:**
- PWA push notifications (future)
- Unsubscribe flow

### Phase 4: Integrations (Decentralized Discourse)
**Goal:** Enable community discussion on decentralized platforms

**Implementation:**
- Public read-only API (rate limited, no auth)
- Bluesky integration: auto-create per-city boards/feeds
- ATProto bridges for decentralized civic discourse
- Database access through views, never direct

**Why:** Builds ecosystem, enables innovation, protects core

### Phase 5: B2B Tenancy (Sustainability)
**Goal:** Paid tier for organizations

**Implementation:**
- Tenant accounts with API keys
- Coverage filtering (only their cities)
- Keyword matching on summaries
- Webhooks (push notifications to client systems)
- Usage analytics

**Why:** Sustainable funding for free public tier

### Phase 6: Intelligence Layer (Premium Civic Analysis) - THE UNLOCK
**Goal:** Critical analysis that identifies political theater, verifies claims with real data, and shows what's actually happening

**The Vision:**
Basic summaries tell you WHAT was proposed. Intelligence layer tells you WHY it matters, WHO benefits, and whether it's substance or theater.

**Example (NYC Congestion Pricing Resolution):**

*Basic summary:*
> "Resolution asks MTA to study congestion pricing impacts. Could benefit working-class communities."

*Intelligence layer (4-line narrative):*
> **Political theater from outer-borough reps doing damage control.** Sponsors backed losing mayoral candidate, got demoted from committees, now asking MTA to study its own program one month post-launch. Real data shows congestion pricing working (speeds up 15%, delays down 25%, $216M revenue funding transit). Resolution is non-binding, costs nothing, promises nothing. If they wanted accountability, they'd fund independent research.

**Architecture: Post-Processing Intelligence (Separate from Pipeline)**

**CRITICAL INSIGHT:** Intelligence layer operates on *existing summaries*, not in the processing pipeline.

```
# Processing pipeline (UNCHANGED):
pdf_text → summarizer → store summary → DONE

# Intelligence layer (SEPARATE SERVICE):
read summary from DB → researcher → judge → store analysis

This means:
✅ Zero changes to stable processing pipeline
✅ Can analyze all 10K existing meetings retroactively
✅ Independent scaling and rate limits
✅ True modularity (separate codebase even)
```

**Three-Agent Architecture:**

```
Agent 1: SUMMARIZER (Already exists)
  Model: gemini-2.0-flash-lite (batch processing)
  Role: Fast, cheap, factual extraction
  Output: Stored in meetings/agenda_items tables
  Status: Production, stable, don't touch

Agent 2: RESEARCHER (NEW - operates on existing summaries)
  Model: gemini-2.0-flash-thinking (extended thinking)
  Tools: Brave Search API, internal DB (sponsor history, voting records)
  Input: Summary from database
  Role: Answer questions with verifiable data
  Output: Real measured outcomes, sponsor context, similar resolutions

Agent 3: JUDGE/ANALYST (NEW - operates on summary + research)
  Model: gemini-2.0-flash-thinking-exp OR claude-sonnet-4
  Input: Summary + research findings
  Role: Adversarial critique, identify theater, question framing
  Output: 4-line narrative + detailed reasoning + confidence score
  Loop: Max 3 research iterations until confidence > 8/10
```

**Source Verification Requirements (NON-NEGOTIABLE):**

1. **Explicit citation for every claim**
   - Link to source (New York Governor press release, NBER study, Wikipedia)
   - Date published (ensure relevance)
   - Entity match (sponsor names, city names, locations match)
   - Outlet reputation (government data > academic > news > blogs)

2. **Replication = Validation**
   - Single source = flag as "unverified claim"
   - Two+ independent sources = "confirmed"
   - Government data + academic research = "high confidence"
   - Contradictory sources = "disputed, needs human review"

3. **No hallucination tolerance**
   - If researcher can't find data, say "data unavailable"
   - If dates don't match, reject source
   - If names don't match, reject source
   - Model outputs "I don't know" over speculation

4. **Audit trail**
   - Store all search queries + results in database
   - Frontend displays sources with collapse/expand
   - Users can verify claims themselves
   - Build trust through transparency

**Output Format:**

```json
{
  "narrative": "4-line punchy analysis showing what's actually happening",
  "key_findings": [
    {
      "claim": "Speeds increased 15% in congestion zone",
      "sources": [
        {
          "url": "https://www.nber.org/...",
          "title": "NYC Congestion Pricing: Early Results",
          "date": "2025-06-15",
          "outlet": "National Bureau of Economic Research",
          "reputation": "high",
          "excerpt": "Average speeds increased from 8.2 to 9.7 mph..."
        }
      ],
      "replication": "confirmed",  // 2+ sources
      "confidence": "high"
    }
  ],
  "detailed_reasoning": "Sponsors Brooks-Powers, Schulman, Farias all backed Andrew Cuomo for mayor in March 2025. After Cuomo lost badly and they got demoted from committees in April...",
  "political_context": {
    "sponsors": [
      {
        "name": "Brooks-Powers",
        "district": "31 (Far Rockaway, Queens)",
        "voting_history": "Opposed congestion pricing in 2024 votes",
        "recent_actions": "Removed from Transportation Committee April 2025"
      }
    ]
  },
  "confidence_overall": 9,
  "research_queries": [
    "NYC congestion pricing measured impacts 2025",
    "Brooks-Powers voting history congestion pricing",
    "NYC council member demotions April 2025"
  ]
}
```

**Premium Tier Pricing:**

*Free Tier (current):*
- Basic summaries (what's proposed)
- Topic tagging
- Search across all cities

*Premium Tier ($99-299/month):*
- 4-line critical narratives
- Source-verified claims
- Sponsor voting history
- Political context
- Detailed reasoning
- Outcome tracking
- Confidence scoring

*B2B/Enterprise:*
- API access: $0.10 per premium analysis
- Regional packages: Bay Area cluster $2K/month
- Custom integrations: Webhooks, data feeds
- Dataset licensing: Contact for pricing

**Use Cases:**

- **Journalists** - Track political theater vs substance across cities
- **Advocacy orgs** - Identify real policy changes vs performative resolutions
- **Law firms** - Monitor zoning/development with verified outcomes
- **Political campaigns** - Opposition research with source verification
- **Researchers** - Civic intelligence dataset for AI training

**Dataset Value:**

With intelligence layer, the dataset becomes:
```
Raw PDF → Basic Summary → Research Phase → Critical Analysis
                              ↓                    ↓
                    [Web search results]    [Adversarial reasoning]
                    [Sponsor context]       [Political dynamics]
                    [Measured outcomes]     [Confidence scoring]
```

This is **multi-hop reasoning training data at civic scale** with verifiable outcomes. AI research labs would pay for this - it's not just document summarization, it's real-world research → synthesis → critique.

**Code Structure (Separate Service):**

```python
# analysis/intelligence/  (NEW module, separate from processing)
├── researcher.py        # Web search + DB context queries (300 lines)
├── judge.py            # Adversarial analysis + narratives (250 lines)
├── verifier.py         # Source verification logic (150 lines)
└── prompts_intel.json  # Researcher + judge prompts

# scripts/
├── analyze_meetings.py # Batch analyzer (reads summaries, calls intelligence layer)
└── validate_analysis.py # Human validation tool

# database/db.py
└── meeting_analysis table + queries (50 new lines)

# server/main.py
└── API endpoint modifications (30 lines - join meeting_analysis)

# ZERO changes to:
pipeline/conductor.py    # Processing pipeline untouched
pipeline/processor.py    # Stays stable
```

**Implementation Phases:**

*Phase 6.1: Researcher Agent (Foundation)*
- Build web search integration (Brave API)
- Build internal DB queries (sponsor history, related resolutions)
- Source verification logic (date/name/location matching)
- Replication tracking (count independent sources)
- ~300 lines in `analysis/intelligence/researcher.py`

*Phase 6.2: Judge Agent (Critical Analysis)*
- Adversarial prompting (question framing, identify theater)
- 4-line narrative generation
- Confidence scoring
- Research question generation (loop back to researcher)
- ~250 lines in `analysis/intelligence/judge.py`

*Phase 6.3: Batch Processing*
- Standalone script `scripts/analyze_meetings.py`
- Reads existing summaries from DB
- Researcher → judge loop (max 3 iterations)
- Stores analysis in `meeting_analysis` table
- Can process all 10K existing meetings retroactively
- ~200 lines for batch script

*Phase 6.4: API Integration*
- Add `meeting_analysis` table to database
- Modify API to join meetings + meeting_analysis
- Return analysis for premium tier
- ~50 lines DB changes, ~30 lines API changes

*Phase 6.5: Frontend*
- Display 4-line narrative prominently
- Collapsible detailed reasoning
- Source links with reputation badges
- "Verify this claim" buttons
- Political context cards (sponsor voting history)

**Technical Requirements:**

- `meeting_analysis` table (critical_analysis, research_findings, sources, confidence)
- Standalone batch processing script (`scripts/analyze_meetings.py`)
- Brave Search API ($5/month for 2K queries)
- Extended thinking budgets (10-20 bullets for judge)
- Audit logging (all queries + results)
- Rate limiting (respect API quotas)
- **Zero changes to existing processing pipeline** (conductor, processor stay untouched)

**Cost Estimate:**
- Basic summary: $0.02 per meeting (current)
- Premium intelligence: $0.11 per meeting (5x more expensive)
- For 10K meetings: $1,100 total (vs $200 basic)
- **Revenue potential:** $99/month × 100 customers = $9,900/month
- **Margin:** ~$8,800/month after processing costs

**Why This Works:**

✅ **Technically proven** - Manual test took 3 minutes, showed 10x value increase
✅ **Post-processing architecture** - Operates on existing summaries, zero pipeline changes
✅ **Retroactive analysis** - Can analyze all 10K existing meetings immediately
✅ **Backwards compatible** - Free tier keeps working, premium is additive
✅ **Independent scaling** - Different rate limits, costs, models from base processing
✅ **Clear monetization** - B2B customers will pay for verified intelligence
✅ **Dataset value** - Training data for AI reasoning models
✅ **Mission-aligned** - Identifies political theater, serves transparency

**The Unlock:**

This transforms Engagic from "nice civic tool" to "civic intelligence platform that shows you what's actually happening."

---

## Next Frontiers (June 2026)

Four directions identified after the extraction-telemetry arc (CHANGELOG
2026-06-11). Each builds on infrastructure that already exists; none are
scheduled — they're the shelf to pull from when the current layer settles.

### Spatial Layer — proximity-based civic alerts

PostGIS is installed and idle: `jurisdictions.geom`, `census_places`,
`zipcodes` all exist, and nothing extracts locations from items. Land-use
items — the highest-stakes civic category — are inherently spatial (hearing
notices carry literal `Location:` fields and street addresses). The cheap
unlock: Gemini already reads every item, so adding a `location` field to the
existing response schema costs **zero extra LLM calls**. Geocode, store as
geometry, and "new development within 1km of your home" becomes an alert
type no other civic platform offers. Likely the highest product-value-per-
effort item on this list.

### Amendment Diffing — the late-addition detector

Agendas get amended (the ground-truth corpus contains a literal "Amended
Agenda"), and the daily re-sync sees both versions but silently overwrites.
The diff is accountability gold: an item added inside the 72h/24h notice
window is the classic move for business someone hopes goes unnoticed.
Machinery shape already exists (attachment hashes, item-level storage,
re-sync flow): detect items added/removed between syncs, flag
`late_addition` inside the notice window, route through the existing alert
system.

### Aftermath Axis — what actually happened

Agendas are pre-meeting; engagic currently says nothing about outcomes
except for API-rich vendors' vote records. Minutes (already visible to
adapters) and Granicus closed captions carry the rest: passed/failed/
continued, tallies, public comment themes. Parsing minutes for outcomes
closes the loop from "this is coming" to "this is what they did" for every
vendor. Same pipeline shape as everything else: fetch → extract → audit →
summarize. Biggest expansion, most work.

### Self-Running Corpus Growth

The extraction-quality method (query the prod failure pool → pull failing
documents → add fixtures → ground-truth by reading the rendered pages →
ratchet recall pins) is a standing loop that an agent can run. A scheduled
weekly routine harvesting `processing_metadata` audits would grow the
chunker/HTML corpora and report ratchets ready to bump — the playbook
becomes a process instead of a session.

**B2B note:** the `tenants` / `tenant_coverage` / `tenant_keywords` tables
mean the Phase 5/6 commercial layer is half-built in the schema already.
Spatial alerts and amendment detection are precisely the features policy
shops, housing orgs, and newsrooms would pay for — they slot into Phase 6's
intelligence layer rather than competing with it.

---

## Technical Principles

### Reliability
- Government data sources are fragile; build defensively
- Cache aggressively, invalidate intelligently
- Fail-fast processing (single tier for free, premium archived for paid)
- Make failure states informative, not cryptic

### Privacy
- Minimal data collection (search queries only, not stored)
- No user tracking beyond opt-in subscriptions
- Open source builds trust in civic applications
- IP addresses not permanently stored

### Scalability
- Start simple (SQLite) but plan for growth
- Measure first, add complexity only when metrics demand it
- Design services to be stateless for horizontal scaling
- Cost awareness: Cloud costs can kill civic projects

### Architecture
- Core data layer owns the truth, others consume through APIs
- Protect the source of truth (no direct DB access for integrations)
- Layer features on top, don't couple them to core
- Each layer can scale independently

### Verification & Source Provenance (Intelligence Layer)
**Gold standard: No claim without citation. Replication is validation.**

**Source Verification Protocol:**
1. **Entity matching** - Sponsor names, city names, dates must match exactly
2. **Outlet reputation** - Government data > Academic > Established news > Blogs
3. **Date relevance** - Reject outdated sources, flag timestamp mismatches
4. **Link validation** - Every claim has clickable source URL
5. **Excerpt storage** - Store relevant text from source (not just link)

**Replication Requirements:**
- Single source = "Unverified claim - needs confirmation"
- Two independent sources = "Confirmed"
- Government + academic = "High confidence"
- Contradictory sources = "Disputed - human review required"

**No Hallucination Tolerance:**
- If data unavailable, output "Data unavailable" (not speculation)
- If dates don't match, reject source entirely
- If names don't match, reject source entirely
- "I don't know" is always acceptable

**Audit Trail:**
- Store every search query + results in database
- Log source selection reasoning
- Expose verification process to users (transparency builds trust)
- Enable users to verify claims themselves

**Why this matters:**
Automated civic analysis is powerful but dangerous. One hallucinated claim about a politician could cause real harm. Source verification isn't optional - it's the foundation of trustworthy civic intelligence.

---

## Success Metrics

### Core Data Layer (Data Quality)
- [x] Multi-vendor adapter system operational (6 platforms, BaseAdapter pattern)
- [x] Item-level processing working (374+ cities, 58% coverage)
- [x] Directory reorganization complete (6 logical clusters)
- [x] Contact info parsing (email/phone/virtual URLs)
- [x] Topic extraction and normalization (16 canonical topics, ready for deployment)
- [ ] AI thinking traces exposed (optional, deferred)
- [ ] 1,000+ cities covered

### Userland (Engagement)
- [ ] User profiles launched
- [ ] 1,000 email subscribers
- [ ] 10,000 email subscribers
- [ ] Weekly digest sent
- [ ] PWA notifications working
- [ ] 50% weekly active users

### Integrations (Ecosystem)
- [ ] Public API documented
- [ ] Bluesky integration live
- [ ] 10+ third-party apps using API
- [ ] Decentralized civic discourse boards

### Tenancy (Sustainability)
- [ ] First paying customer
- [ ] $1,000 MRR (covers infrastructure)
- [ ] $10,000 MRR (sustainable)
- [ ] Tenant webhooks working

### Intelligence Layer (Premium)
- [ ] Researcher agent operational (web search + DB context)
- [ ] Judge agent producing critical analysis
- [ ] Source verification working (entity matching, replication tracking)
- [ ] 4-line narratives with 8+ confidence
- [ ] 100 premium analyses validated by humans
- [ ] First premium tier customer
- [ ] Dataset licensing conversation with AI lab

---

## Key Learnings

### What Worked
- **BaseAdapter pattern** - compounds value with each new vendor (6 platforms, 94% success)
- **city_banana identifier** - vendor-agnostic, eliminates coupling
- **Priority queue** - improves user experience (recent meetings first)
- **Item-level processing** - better summaries, granular topics, batch API cost savings (50%)
- **HTML agenda parsing** - reusable pattern across vendors (Granicus breakthrough: +200 cities)
- **Item-first architecture** - backend stores data, frontend composes display (separation of concerns)
- **Directory reorganization** - 6 logical clusters, tab-autocomplete friendly (-292 lines)
- **Topic normalization** - AI variations → 16 canonical topics (consistent search/alerts)
- **Processor modularization** - dramatic complexity reduction (-1,549 lines across refactors)
- **Prompts as JSON** - rapid iteration without code deployment
- **Cache-first** - instant API responses, decouples scraping from serving

### Patterns to Replicate
- Single unified method with optional parameters (`get_city()`)
- Extract shared logic to base class, keep subclasses thin
- Fail-fast with archived premium tiers for future revenue
- Hybrid architecture: Python for business logic, Rust for infrastructure (future)
- Structured logging with scannable tags (`[Cache]`, `[Vendor]`, `[Topic]`)
- Separate layers with clean interfaces (data layer → userland → integrations → tenancy)
- Backend stores data, frontend composes display (separation of concerns)

### Remaining Challenges
- **User accounts/auth** - Magic links? Email-only? Social auth?
- **Email infrastructure** - SendGrid vs self-hosted (cost/deliverability trade-offs)
- **PWA notifications** - Service worker setup, iOS support limitations
- **Frontend quick wins** - Participation info display, topic badges (backend ready)
- **Alert matching logic** - Topic-based subscriptions, frequency preferences
- **Scaling email alerts** - Batch processing for 1,000+ subscribers
- **Bluesky/ATProto** - Integration patterns for decentralized civic discourse

---

## The Path Forward

**Immediate (November 2025):**
1. ✅ Topic extraction deployed and live
2. ✅ Participation info deployed and live
3. Frontend topic filtering (backend ready)
4. Basic user profiles (email + city + topics)

**Near-term (Q1 2026):**
5. Email alert system (topic matching)
6. Weekly digest emails
7. PWA push notifications
8. Public read-only API documentation

**Medium-term (Q2 2026):**
9. Bluesky/ATProto integrations
10. B2B tenancy with webhooks
11. Remaining vendor item-level processing (CivicClerk, NovusAgenda, CivicPlus)

**Intelligence Layer (Phase 6 - THE BIG LIFT):**

*Proof of Concept (1-2 weeks):*
- Build researcher agent (Brave API, DB queries, source verification)
- Build judge agent (adversarial analysis, 4-line narratives, confidence scoring)
- Create batch analyzer script (reads existing summaries from DB)
- Test on 10 manually selected meetings (controversial/political theater)
- Validate: Does it match manual analysis quality?
- **Key insight:** This operates on existing summaries, no pipeline changes needed

*Limited Beta (2-4 weeks):*
- Process 100 existing meetings with intelligence layer
- Add `meeting_analysis` table to database
- Track costs, quality, failure modes
- Refine prompts based on edge cases
- Get 5-10 beta user feedback
- **Pipeline stays untouched** - this is pure post-processing

*Premium Launch (4-6 weeks):*
- Enable for select high-value cities (SF, NYC, LA, DC)
- API returns analysis for premium tier
- Frontend displays 4-line narratives + sources
- Pricing tiers finalized ($99-299/month)

*Monetization (8+ weeks):*
- B2B outreach with concrete examples
- Self-service upgrade flow
- Dataset licensing conversations with AI labs
- Regional packages (Bay Area cluster, etc.)

**Long-term (2026+):**
- 1,000+ cities covered
- Rust conductor migration
- Intelligence layer becomes the standard (free tier gets basic, premium gets critical analysis)

**Philosophy:** Move slow and heal things. Build trust through transparency. Design for civilians, not insiders. Open source the data layer, monetize the intelligence on top. **Source verification is non-negotiable** - one hallucinated claim destroys trust.

---

**"The best time to plant a tree was 20 years ago. The second best time is now."**

Let's make local government accessible to everyone.
