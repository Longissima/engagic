# Debt-Class Radar

**Status:** Analysis (2026-06-29), from a 3-scout read-only hunt across `/opt/engagic`
and `/opt/motioncount`. Companion to `TECH_DEBT.md` (instance register) and
`ARCHITECTURE_REVIEW.md`. This doc is the *lens*, not a new register: it groups debt by
**pattern** so we fix classes, not instances.

## The method

When we hit a problem, abstract the instance to the pattern, then hunt the pattern
elsewhere. The 2026-06-29 sync freeze (one unguarded `get_text`) generalized to:
**"a capability solved at N call sites with N levels of rigor"** = a missing/leaky
abstraction. Hunting that across both repos surfaced four major instances plus two
concrete bugs. The payoff is the radar below — a reusable diagnostic.

## The pattern has three sub-flavors (each a different fix)

| Flavor | Looks like | Fix verb |
|---|---|---|
| **Missing** | solved ad hoc at each site, no shared thing | **build** it |
| **Siloed** | a *good* abstraction exists, walled into one corner | **adopt** it everywhere |
| **Drifted copy** | same capability copied, then diverged | **consolidate** the copies |

**Two tells to scan for:** (1) a capability implemented at N call sites with N rigor
levels → missing/leaky abstraction; (2) boundaries drawn around *jobs/scripts* instead
of *responsibilities* → organic, not designed.

**Counter-example (the template):** entity-ID generation is fully centralized in
`database/id_generation.py` — one place, consistent rules (meeting MD5/8,
matter/member/committee SHA256/16). Proof we can do it right. The goal for the items
below: "do for HTTP and extraction what we already did for IDs."

---

## Ranked instances (risk × leverage)

### 1. HTTP / document acquisition — *Siloed + Missing* — highest leverage
22 distinct fetch/download sites, 5 libraries (aiohttp, httpx, requests, curl_cffi,
urllib), **retry at 2, rate-limiting at 3** (all engagic vendor-path; **zero in
motioncount**).
- **Good abstraction, siloed:** `vendors/session_manager_async.py` (`AsyncSessionManager`,
  pooled per-vendor sessions) + `vendors/rate_limiter_async.py` (`AsyncRateLimiter`,
  per-vendor slots/delays). Used by `base_adapter_async.py:114-250` (3× backoff,
  Retry-After), `analyzer_async.py`, `fetcher`. NOT used by email, turnstile, or any
  motioncount code.
- **Reinvented / naked:** `parsing/pdf.py:761` (requests, no retry), `pipeline/utils.py:226`
  (HEAD, 3s), motioncount `outreach_engine/enrich.py:196/297/355` (Apollo/Hunter/Mailgun,
  **no retry, no rate-limit**), `prospecting/scraper.py:141`, `intelligence/reanalyze.py:138-152`.
- **Live risk:** `enrich.py` can hammer a third-party API uncoordinated → ban — same
  failure class as the 2026-06-29 civicplus throttle, minus the politeness.
- **Fix:** extract engagic's session+rate-limit pair into a shared client; adopt it in the
  rest of engagic and in motioncount.

### 2. Heavy/crash-prone work guards — *Siloed* — highest risk
One hardened extraction path vs ~5 PDF-parse paths running **naked in-process**.
- **Guarded (the model):** `analysis/analyzer_async.py:89-176` — `multiprocessing` subprocess,
  `RLIMIT_AS≈1.5GB`, OOM score +500, 600s timeout, kill-on-timeout. Plus
  `parsing/pdf.py:400-468` (megapixel ceiling, tesseract 60s timeout).
- **Unguarded landmines:** `vendors/adapters/parsers/router.py:268,306` and `:242-244`;
  `agenda_chunker.py:1598-1749`; `agenda_chunker_v2.py:978+`; `agenda_chunker_template.py:196,245,455,886,1096`
  — all dispatched via `base_adapter_async.py:581` `asyncio.to_thread(chunk_pdf)`: a
  **thread pool, which cannot set RLIMIT and cannot be hard-interrupted.** Also motioncount
  `intelligence/reanalyze.py:97-110` (thread-isolated only, no process/RLIMIT) and
  `outreach_engine/select.py:64-66` (inline).
- **This is why 2026-06-29 happened** — the freeze was the *designed behavior of the entire
  chunking lane*, not a fluke. See `[[project_sync_chunker_freeze]]`.
- **Fix:** route all heavy PDF work through the subprocess-guarded path; the corpus
  restage (`CORPUS_ARCHITECTURE.md`) collapses extraction into one guarded stage anyway.

### 3. Identity / hashing — *Drifted* — contains two real bugs
Entity IDs are clean (the counter-example), but everything downstream drifted.
- **Attachment hashing fork:** `pipeline/utils.py` `hash_attachments_fast` (56-78),
  `hash_attachments_with_metadata` (98-141), `hash_substantive_attachments` (155-190),
  `hash_attachments_fast_legacy` (81-95). Same set hashes differently by which fn is
  called → silent false "changed" if `fast`↔`with_metadata` mix. `sv1:` versioning
  mitigates legacy only.
- **Signed-URL assumption:** `attachment_identity` (`utils.py:37-53`) strips query params
  only for a hardcoded marker list `{sig, x-amz-signature, signature, awsaccesskeyid}`.
  Vendor auth schemes outside that list → false "changed."
- **RISK — brief_runs dedup is unversioned:** `intelligence/pipeline.py:162-295` dedups by
  `item_id` set membership with no version tag (contrast attachment hashing's `sv1:`). If
  `item_id` generation ever drifts, dedup breaks **silently** even with complete rows. NOTE:
  this is a *distinct* failure mode from the documented ~65% candidate-row loss
  (`project_brief_runs_audit`), which is a row-completeness / write-atomicity issue — missing
  rows, not identity drift. Both corrupt the same "what's new" math by different mechanisms;
  both live in the same unversioned-dedup subsystem.
- **BUG — IP hashing diverges:** `motioncount/server/demo.py:76-77` hashes the **full IP**
  (32 hex, no bucket), while `content_gate.py:34-45`, `auth.py:46-60`,
  `utils/anon_tracking.py:57-66` all hash the **/24** (IPv6 /48). Demo quota identity is
  therefore decoupled from signup/abuse tracking. (engagic `server/rate_limiter.py` uses a
  third scheme: SHA256(ip), 12 hex, no salt.) ~5-line fix to align demo.py to /24.
- **No file-content hashing exists anywhere** — confirms the corpus byte-hash
  (`CORPUS_ARCHITECTURE.md`) is a genuinely new primitive, not a duplicate.

### 4. LLM / OCR invocation + cost accounting — *Siloed + Drifted* — violates a written rule
- **Gemini:** `analysis/llm/summarizer.py:165-208` is rigorous (retry=4, jittered backoff,
  180s cap, parses Gemini `retryDelay`). motioncount `intelligence/investigative.py:199-205`
  (no retry/timeout, hardcoded `gemini-2.0-flash`), `stream.py:276`, `profile_gen.py:233/291`,
  `filter_direct.py:72` (SDK-default only), `scripts/la_policy_dump.py:161-172` (manual retry).
- **Anthropic:** `outreach_engine/_batches.py:96-127` has a 5-attempt + 300s wrapper that
  `verify`/`select`/`draft` inherit; `intelligence/reanalyze.py:267-386` (max_retries=3,
  120s, no wrapper), `filter.py:284-289` (3/120s), `extraction/extract.py:55-66` and
  `backfill.py:117` (no explicit retry). "Abstraction exists, half-adopted."
- **Cost accounting:** CLAUDE.md mandates "log LLM/grounding usage." engagic summarizer has
  it but **hardcoded + stale** ("Gemini pricing as of Nov 2025," `TECH_DEBT.md:235`);
  motioncount has none. Not met uniformly.

---

## New vs. already in the register

`TECH_DEBT.md` is sharp but lists these as *separate* items without naming the class:
- **Already documented (same flavors, unlabeled):** scanned-PDF redline loss (`TECH_DEBT.md:93`,
  Mount Airy NC — also the open question in `CORPUS_ARCHITECTURE.md`; they converge),
  fetcher dead-code retry (`:58`), unconditional vendor sleep (`:13`), Gemini pricing stale
  (`:235`), `AsyncSessionManager` noted good (`ARCHITECTURE_REVIEW.md:186`), two-chunkers
  resolved (`:204`), email-vs-IP rate-limit storage split (`ARCHITECTURE_REVIEW.md:150`).
- **New here:** guard-asymmetry as a *class* (5 unguarded paths via thread pool); the 22-site
  HTTP scatter + motioncount having no client at all; the **demo.py IP-hash bug**; attachment
  fast-vs-slow drift; the **brief_runs versioning gap**; no-file-content-hashing.

## Two cheap wins (do regardless of the big refactors)

1. **Align `demo.py` IP hashing** to /24 (match the other three). ~5 lines, real abuse-tracking
   inconsistency, zero architectural risk.
2. **Investigate the brief_runs versioning gap** as the root cause of the ~65% candidate-loss
   scar — a concrete lead on an existing mystery.

## Using the radar going forward

For any capability, ask which flavor it is — **missing** (build), **siloed** (adopt), or
**drifted** (consolidate) — and whether its boundary follows a *job* or a *responsibility*.
The big refactors (shared HTTP client, one guarded extraction stage) are project-sized and
belong in `CORPUS_ARCHITECTURE.md`'s trajectory; this doc is the standing checklist of where
the pattern lives.
