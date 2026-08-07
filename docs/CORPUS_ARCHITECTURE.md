# Ground-Truth Corpus & Pipeline Restaging

**Status:** Proposal (2026-06-29); slices 1-3 SHIPPED (2026-07-02, see
CHANGELOG "The Corpus Exists", "The Collapse", "One Write Path"). Implemented
in this tree (with migrations 032-035 applied to production on 2026-08-07 as
recorded in `PIPELINE_REMEDIATION.md`):
the sha256(bytes) primitive, document_blob/document_source (migration 025;
source freshness state in migrations 027 and 034 and bounded ingest failures in
migration 028),
the engagic-corpus R2 bucket over the S3 data plane, the `corpus/` package,
the shared typed `DocumentSourceAcquirer`, the shared subprocess guard
(`parsing/subprocess_guard.py`), and one ground-truth producer function
(`pipeline/ground_truth.py`: archive -> guarded chunk -> text persist) called by
both sync adapters and the processor's claim-time shape manufacturing
(`_manufacture_items` -> `attach_items` funnel). Sync-side chunking
is legacy behavior behind ENGAGIC_SYNC_CHUNKING (default true); deferred mode
makes sync pure stage 1 (archive + enqueue) with shape born at claim time
from corpus bytes. PyMuPDF executes in exactly two guarded child targets
(chunk_pdf, parsing.pdf.extract_document_file) behind one guard and one
producer. Still pending: flip SYNC_CHUNKING per-vendor (granicus last -- its
chunk sites double as URL probes), OCR offload (phase 0 Mistral), motioncount
reading from the corpus, merging the two child targets.
**Motivation:** (1) a single pathological PDF froze the whole sync for ~15 min on
2026-06-29 (unguarded `get_text` in the sync chunker); (2) extraction work is
re-done on every reanalysis because we extract-and-discard; (3) the OCR RAM bomb
caps throughput on a 3.8 GB box; (4) motioncount links PyMuPDF directly, which we
want to eliminate (licensing + re-work). All four collapse into one architecture.

---

## Target law (partially shipped)

> **Extraction writes once. Everything downstream reads.**
> One stage produces text and is the sole system of record (DB structure + R2
> corpus). `process` (summarize) is a pure reader. `motioncount` (reanalyze) is a
> pure reader. Nobody re-downloads, nobody re-parses, and PyMuPDF executes in
> exactly one place.

Everything below is the mechanics of that law.

The live pipeline now has shared typed acquisition, content-addressed originals,
guarded extraction, and one ground-truth write path. `process` can read archived
artifacts but still manufactures missing shape/text at claim time; `motioncount`
has not yet become a corpus-only reader. Treat the quoted law as the destination,
not a claim that every consumer has already crossed over.

---

## The invariant: stage 2 *manufactures* ground truth, it doesn't consume it

Tempting fallacy: "we operate on ground-truth text." We don't — not at the input.
The inputs are heterogeneous (born-digital item PDF, multi-item packet, scanned
pages). **Raw original text is the *output* of the produce-text stage, not its
input.** That stage's entire job is to manufacture one uniform invariant —
*raw original text (+ item delineation)* — out of heterogeneous sources. Once it
exists, every consumer operates on the same artifact.

Naming the stage after its output ("produce raw original text") rather than its
mechanism ("pymupdf vs ocr") is deliberate: the mechanism is heterogeneous, the
output contract is uniform.

### OCR is per-page, not a document category

Critical correction to any "text docs vs OCR docs" routing model: **OCR is a
per-page fallback *inside* extraction**, triggered when a page's text layer is
absent/insufficient (`ocr_threshold` = min chars/page, `parsing/pdf.py`). A single
attachment is commonly 90% text-layer + a few scanned pages. So the real spectrum is:

- item attachment, **no** OCR load (pure text layer) — the cheap majority
- item attachment, **some** OCR load (a few scanned pages mixed in) — the long tail
- **OCR-only** attachment (fully scanned) — the rare expensive case

Consequences:
- You **cannot** route whole documents to an "OCR pipeline." The OCR decision
  lives per-page inside stage 2; stage 2's *output* is uniform regardless.
- Offloading OCR to an API/GPU speeds up the **mixed tail**, not just fully-scanned
  docs — which is most of where time actually goes.
- The OCR engine is a swappable per-page component (Tesseract today → VLM/API later;
  see open questions). It does not change the stage boundaries.

---

## Stages (grouped by resource profile + failure domain)

| Stage | Work | Bound by | Concurrency | System of record |
|---|---|---|---|---|
| **1. Acquire & Archive** | fetch metadata; download original bytes; **content-hash**; **dedup gate**; archive original → R2 | network I/O | high | DB rows + R2 `originals/` |
| **2. Produce Ground Truth** | bytes → raw text (+ chunk delineation), per-page OCR fallback; persist text → R2 + DB | CPU/RAM (local OCR) or network (offloaded OCR) | **throttled to RAM budget** | R2 `text/` + DB pointer/provenance |
| **3. Summarize** | raw text → LLM | LLM API / rate limit | high | DB summaries (already exists) |

Target readers of stage 2's output are **`process` summarize** and
**`motioncount` reanalyze**. Process already shares corpus-first acquisition and
guarded extraction; motioncount conversion remains pending.

### Why download belongs in stage 1, not stage 2

Two reasons to *not* fold download into "produce text":
1. **Dedup before the expensive stage.** Content-hash the bytes at archive time; if
   that hash was already extracted, **skip stage 2 entirely**. Bundling download
   into stage 2 buries the gate inside the bottleneck.
2. **Resource scaling.** Download is cheap I/O that wants wide concurrency;
   extraction is RAM-bound and must stay throttled. Different knobs → different stages.

The content hash is a twofer: the dedup key *and* the corpus content-address key.
The shipped acquisition boundary computes it from observed source bytes.

### Heterogeneous internals of stage 2 (uniform output)

- **Item PDF** (attachment == one item): item bounds are the PDF cluster itself →
  straight PyMuPDF text, no chunking. Immediately usable.
- **Packet PDF** (many items in one file): item delineation lives in the PDF
  (TOC/structure) → must chunk *while* extracting (as today, `agenda_chunker*`).
  Output: full raw text **plus** the item-boundary map.
- **Per-page OCR fallback**: any page of either type lacking a text layer →
  OCR engine. Folds into the same pass.

All three emit the same contract: `{ raw_text, item_delineation?, per_page_method[] }`.

---

## Storage design (target, with corpus tables shipped)

The content-addressed `document_blob`/`document_source` layer described here is
live. Direct `content_sha256` references from every meeting/item row remain part
of the target design; current consumers resolve archived revisions through the
shared source-identity acquisition boundary.

**R2** (reuse existing plumbing: `scripts/generate_tiles.py:251-310`, wrangler CLI,
creds `CLOUDFLARE_API_TOKEN`/`CLOUDFLARE_ACCOUNT_ID` in `.llm_secrets`). New bucket(s):

```
engagic-corpus/
  originals/<sha256(bytes)>          # archived source bytes; media type is metadata
  text/<sha256(bytes)>.txt|.md       # extracted raw text (+ markdown if Layout/VLM)
```

Content-addressed by **hash of the source bytes** → automatic dedup, immutable,
re-extraction-safe.

### Source freshness contract

Content identity and source identity answer different questions. The R2 key says
which immutable bytes we have; `document_source.source_identity` says where those
bytes were observed. A stable municipal URL can later serve different bytes, so a
corpus hit is not permission to treat that source as permanently fresh.

Migration 034 gives each `(content_sha256, source_identity)` association
separate state:

- `last_observed_at`: the pipeline used this archived revision, including a cache hit;
- `last_validated_at`: the origin confirmed this revision with HTTP 200 or 304;
- `last_validation_attempt_at`: the last attempted check, used to back off during an
  origin outage;
- `etag` and `last_modified`: conditional-request validators when the host provides
  them.

The acquisition boundary serves a recently validated corpus original without
network traffic. After `ENGAGIC_CORPUS_REVALIDATE_SECONDS` (24 hours by default),
it performs a conditional request. HTTP 304 advances validation without downloading
the body; HTTP 200 is content-hashed and archives a new immutable revision when the
bytes changed. A failed validation serves the archived revision and waits
`ENGAGIC_CORPUS_REVALIDATE_FAILURE_SECONDS` (one hour by default) before retrying.
This fail-open policy keeps civic-host outages from blocking summaries or producing a
request storm.

Concurrent acquisition of the same signature-stripped source identity is
single-flight within a process. Content addressing remains the cross-process
convergence mechanism.

Revalidation is demand-driven: the TTL is evaluated the next time a consumer
acquires that source. It is not a background expiry scheduler, so a fully
processed URL that is never requested again is not swept automatically.

**DB** (pointers + provenance; big blobs never inline in hot rows):

```
document_blob (
  content_sha256   text primary key,   -- hash of source bytes
  original_key     text,               -- R2 originals/<hash>
  text_key         text,               -- R2 text/<hash>.txt
  extract_method   text,               -- 'pymupdf' | 'pymupdf+layout' | 'vlm:<model>' | mixed
  extract_version  text,               -- bump to force selective re-extraction
  page_count       int,
  ocr_page_count   int,                -- how many pages hit the OCR fallback
  bytes            bigint,
  created_at       timestamptz
)
```

Existing rows (`meetings.{agenda_url,packet_url}`, `items.attachments[]`) gain a
`content_sha256` reference to `document_blob`. Provenance (`extract_method` +
`extract_version`) is mandatory: a corpus without "how was this produced" rots, and
we *will* re-extract when the extractor upgrades (Tesseract → VLM, Layout adoption).

## Historical 2026-06-29 gap analysis

The next two sections preserve the evidence and sequencing that motivated the
shipped corpus/acquisition work. Their present-tense statements describe the
proposal-time system, not the current implementation summarized above.

### Byte-hash primitive (shipped)

At proposal time the pipeline hashed metadata (`pipeline/utils.py`
`hash_attachments_*` = URL+name; `id_generation.py` = metadata keys), not file
bytes. The new primitive was `sha256(downloaded_bytes)` in stage 1. It is now
shipped in the corpus acquisition path; metadata/work hashes remain separate
desired-work identities.

---

### Original tee-point problem and sequencing

**There was no single seam on 2026-06-29** — text was produced in two jobs on opposite sides of
the queue, which is the core reason persistence isn't a one-line drop-in:
- **sync chunker** — `vendors/adapters/parsers/router.py` `chunk_pdf` →
  `agenda_chunker_v2` (downloads + `get_text`s agenda packets for item structure +
  memo text). The *unguarded* site (2026-06-29 freeze).
- **process attachment extraction** — `analysis/analyzer_async.py:404-412`
  (download → temp file → subprocess extract → `unlink` at 433). The *guarded* site.

These paths sometimes touched the same bytes (a packet chunked in sync,
re-extracted in process) and sometimes different bytes (separate per-item
attachments). So a naive
`persist_text()` at one site misses the other, and at both sites risks
double-store/divergence — *unless* the corpus is keyed on `sha256(source_bytes)`, which
makes identical bytes dedup automatically and divergence impossible. The proposal
identified stage 2 as the missing chokepoint. The current shared acquisition and
ground-truth owner are the shipped result; the remaining two guarded child targets
are an implementation detail still scheduled for consolidation.

**Sequencing is forced — do not reorder:**

1. **Guard + offload first.** Process extraction was already hardened: isolated
   subprocess, 600 s timeout, `RLIMIT_AS=1GB`, `pdf_semaphore=6`
   (`analyzer_async.py:95-176`). The sync chunker was not — that's why it froze on
   2026-06-29. Before moving any heavy extraction earlier, it had to carry those guards,
   and OCR must move off-box (API/GPU). Otherwise you relocate the RAM bomb into the
   seatbelt-less stage and make the freeze the *normal* case.
2. **Then** move/extend extraction into stage 2 and turn on corpus persistence.
3. **Then** flip `process` and `motioncount` to read from the corpus; delete
   motioncount's `import pymupdf` (`intelligence/reanalyze.py`).

## Decoupling cost: the R2 round-trip is noise

Concern: "stage 2 writes text to R2, stage 3 reads it back minutes later — wasteful?"
No. An R2 read of a text blob is ~tens of ms; summarization is seconds; per-page OCR
is hundreds of ms to seconds. The round-trip is **noise against the stage work**, so
decouple freely for the resilience/independent-scaling win. A write-through
fan-out (hand text to the summarizer in-memory *and* persist async) is a valid
micro-opt but optimizes a non-bottleneck — skip it until measured.

---

## Target consequences (why all four threads collapse)

- **Throughput:** the RAM bomb (OCR) leaves the 3.8 GB box → stage 3 scales on LLM
  concurrency, not swap. Dominant win is OCR-offload, *not* the stage relabeling
  (work is conserved on a shared box; only offload + overlap add capacity).
- **Corpus:** stage 2 writes it as a first-class artifact, not a discarded side effect.
- **motioncount de-licensing:** reanalyze reads corpus text → PyMuPDF exists only in
  engagic (the AGPL-published citizen). See licensing note below.
- **Resilience:** one guarded producer; a pathological page wedges one extract worker
  for ≤ timeout, never the whole pipeline.

## Licensing note

PyMuPDF + `pymupdf4llm` = AGPL-or-commercial; `pymupdf-layout` = Polyform-Noncommercial
-or-commercial (no open-source escape — commercial license mandatory if adopted).
This architecture confines all PyMuPDF execution to engagic (source-published →
AGPL-compliant) and is intended to make motioncount a pure reader of unencumbered
output text. That motioncount conversion remains pending. An
Artifex commercial license scoped to cover both repos is the belt-and-suspenders move
and is independently required for Layout.

## Per-page OCR engine — decided (deep research, 2026-06-29)

Replacing Tesseract for the **scanned-page fallback inside stage 2**. Note OCR is a
*minority* path (text-layer-primary), so "lightest to operate" weighs heavily.

- **Primary workhorse: LightOnOCR-2-1B** — Apache-2.0 (cleanest: Qwen3 + Pixtral
  backbones, no Qwen research-license taint), olmOCR-Bench **83.2** (top-2), fastest
  in cohort (~5.7 pg/s H100), ~2 GB / single consumer GPU, ~$0.14/1k self-hosted.
- **Higher-assurance tier: olmOCR-2-7B** — Apache-2.0, olmOCR-Bench 82.4 (tables 84.9,
  multi-col 83.7), **unit-test reward training = faithfulness-oriented** (rewards
  correct transcription/reading-order over fluent-but-invented text — the right bias
  for a system-of-record). Needs an H100; <$0.20/1k self-hosted.
- **Throughput specialist (bulk backfill only): DeepSeek-OCR** — MIT (most permissive),
  200k+ pg/day/A100, but accuracy cliff (97%→60% at 20× optical compression) +
  hallucination history → use only with verification, never as the faithful default.
- **Zero-ops escape hatch: Mistral OCR (hosted)** — $1/1k batch ($2 list; OCR 4 now
  $2 batch). Strong, but **does NOT preserve character formatting** (bad for redlines)
  and ~10× pricier than self-hosting at scale.

**Adoption path:** Phase 0 — Tesseract → Mistral API (zero infra; OCR RAM bomb leaves
the box immediately; validate quality lift). Phase 1 — self-host LightOnOCR-2-1B on
serverless GPU once volume justifies. olmOCR-2-7B as the higher-assurance tier.

**Confirms the architecture:** *no OCR benchmark evaluates redline semantics and no
model captures them* → for born-digital legislative redlines, extract the PDF text
layer (where strike/underline operators survive) and route only genuinely scanned
pages to OCR. This is the text-layer-primary design above — and the reason **not** to
"OCR everything" (Barrow built a dataset; we build a system of record).

Caveats: fast-moving space (re-verify versions/leaderboard/pricing before committing —
Chandra-2 ~85.8 and others now contend); all quality numbers vendor-self-reported
(olmOCR-Bench is the shared third-party benchmark); VLM OCR can hallucinate on degraded
scans (language priors).

## Open questions (pending)

- **Serverless-GPU economics** unresolved: the Barrow ~$0.30/1k-on-Modal and Textract
  ~$1.50/1k figures did NOT verify; real all-in $/1k for LightOnOCR-2/olmOCR-2 across
  Modal vs Runpod vs Baseten (cold-start, idle, per-sec H100) needs a head-to-head
  benchmark before committing GPU infra. Phase 0 (hosted Mistral) is not blocked by it.
- **Redline semantics on genuinely SCANNED pages** (no text layer to fall back on) —
  unsolved by any model/benchmark; a custom layout/vision post-processor problem.
- **PyMuPDF4LLM + Layout** as the stage-2 born-digital text/structure engine
  (replaces hand-rolled chunker heuristics for the text-layer majority). Licensed.
- Whether stages run as one continuous co-resident process today or get split into
  separate workers/machines (the relabeling is free; the offload is the lever).

---

## Additional considerations — latent step-changes

The meta-pattern behind "persist text" is broader than text:
**anywhere we re-derive something we could own, or discard something we already
produced.** Text was one instance. Others noticed during the 2026-06-29 exploration,
roughly biggest-first. None are required for the corpus work; all share its shape.

1. **The corpus kills link-rot and the signed-URL dance (free consequence).**
   We maintain a whole ephemerality subsystem — `pipeline/url_refresh.py`,
   `AttachmentInfo.url` flagged "ephemeral for signed URLs," and
   `history_id`/`meta_id`/`cc_agenda_id` stable-ID juggling to re-resolve links at
   fetch time. Archiving original *bytes* by content hash makes every archived
   document immune to link rot and SAS expiry forever; the refresh machinery shrinks
   to "only for documents not yet archived." A maintained subsystem mostly evaporates.

2. **Identity-by-bytes makes artifact handling idempotent.** At proposal time the
   pipeline used metadata as a proxy for artifact change/seen. The shipped corpus
   now hashes observed bytes, while `sv1`/`mw1` metadata identities separately
   describe desired summarization work. Byte identity turns
   "processed this exact artifact?" from a guess into a fact → the structural fix for
   the `brief_runs` candidate-row dedup loss *class*, not a patch.

3. **Persist the telemetry stream (partially shipped).** Durable pipeline runs,
   attempts, stages, and Batch rows now back last-hour percentiles and 24-hour
   streaming/Batch series in `engagic-conductor status`. Rich structlog-only vendor,
   OCR, and chunk-quality details are still not a permanent analytics stream; export
   them to a durable table/object store before claiming long-term trend coverage.

4. **A frozen corpus turns eval from archaeology into CI.** Eval culture exists
   ("read the pdfs, score the chunker against reality," the quality layer) but runs
   against *live* data — which is why eval #2's all-HOLD was ambiguous (substrate moved
   vs. code moved). A frozen corpus slice as ground truth makes extractor/chunker
   quality a replayable regression suite: change extractor, re-run, diff. Phase-2 —
   depends on the corpus existing.

5. **The box should be a scheduler, not a worker.** Every sharp edge — OCR RAM bomb,
   the freeze, swap thrash, the 6-concurrent cap, can't-run-`py-spy`-locally — traces
   to a 3.8 GB box doing heavy heterogeneous work *in-process*. Principle: the
   persistent box coordinates; all heavy, spiky, dangerous work runs on elastic compute
   spun up and torn down. OCR-offload is one instance of this; generalize it.

**Discipline note.** The tempting next step after "persist text" is "embed it, go
semantic." The corpus *enables* that, but it's a real fork with a real downside —
direct tension with the keyword-precision / filter-quality bar. Enabled ≠ earned.
Items 1–5 are pure wins; a semantic layer is a deliberate decision, not a freebie.

**The tell, to scan for more:** re-deriving something we could own, or discarding
something we already produced.
