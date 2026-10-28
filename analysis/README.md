# analysis

This package turns document text into summaries and topics. The processor drives it, and it owns three things: the LLM transport, the prompts, and document acquisition and extraction for processing.

## Layout

| File | Role |
|---|---|
| `analyzer_async.py` | `AsyncAnalyzer`: acquires documents (corpus first, download on a miss), extracts them in the guarded child process, and runs summaries for one item, a batch of items, or a whole packet (`process_agenda_with_cache_async`, the monolithic fallback). |
| `llm/summarizer.py` | Loads the prompts, picks reasoning effort, drives the backend, parses and validates responses, and runs the Gemini Batch lane. |
| `llm/backends.py` | `ChatBackend` and its implementations: `ZaiBackend` (production), `OpenRouterBackend` (the bench), `GeminiBackend` (rollback and Batch), `AnthropicBackend`. `build_backend()` picks one from `ENGAGIC_LLM_BACKEND`. |
| `llm/prompts_v3.json` | The live prompts: `item.unified` and `meeting.fallback`. `prompts_v2.json` is kept for comparison. |
| `llm/civic_context.py` | Jurisdiction, body and document inventory given to every summary lane, so the model knows whose meeting it is reading. |
| `llm/input_budget.py`, `llm/document_representation.py` | Request assembly. The provider tokenizer decides what fits. Oversized documents of a recognized shape (catalogs, canvasses, dense tables) get a deterministic, information-preserving representation. Unrecognized prose is never silently truncated; it fails preflight. |
| `topics/` | The canonical taxonomy (`taxonomy.json`: housing, zoning, budget, public safety and so on) and the normalizer that maps model topics onto it. |

## Rules

- **The model follows price times quality.** The summarizer owns prompts, effort and parsing; everything provider-specific lives behind `ChatBackend`. Switching models or providers is a config change (`ENGAGIC_LLM_BACKEND`, `ENGAGIC_PRIMARY_MODEL`). The current default, GLM-5.3-flash on Z.AI, won a price-and-quality bake-off in motioncount. Before any swap, `scripts/compare_llm_backends.py` re-summarizes recent items through each candidate and lays the results beside the stored summaries. It uses the same corpus input and the same summarizer code path, so only the backend differs.
- **Effort tiers by document size.** Low for 10 pages and 30k characters or less, medium up to 50 pages and 150k, high above that, unless `ENGAGIC_LLM_REASONING_EFFORT` pins it.
- **Errors are classified.** `LLMAccountError` (quota, balance, an expired or revoked key) stops the run and writes nothing, so a unit's retry budget is never spent on an account problem. Transient errors retry with a bound. A content rejection is recorded against its unit.
- **Summaries carry `prompts_version`.** `scripts/resummarize_items.py` finds items produced by an older version. Changing a prompt does not invalidate frozen summaries by itself.
- **The corpus holds the truth.** Representations and receipts sent to the model carry source hashes and exact ranges; they never replace or delete corpus text.
- **Batch is Gemini-only.** With the Gemini backend, non-urgent meetings go through the Batch API at half the token cost. On Z.AI every job streams.
