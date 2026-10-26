"""Re-summarize recent items through candidate models and lay the results
side by side with the stored summary.

Same input text (rebuilt from the corpus exactly as the pipeline renders it),
same v3.2 prompt, same production Summarizer code path; only the backend
differs. This is the gate for any model swap: read the output, then decide.

    uv run python scripts/compare_llm_backends.py --limit 50
    uv run python scripts/compare_llm_backends.py --limit 20 \\
        --models zai:glm-5.3-flash,z-ai/glm-5.3-flash,google/gemini-3.1-flash-lite

Writes <out>/results.jsonl (one row per item) and <out>/report.md.
"""

import argparse
import asyncio
import json
import random
import statistics
import time
from pathlib import Path

from analysis.llm.backends import AnthropicBackend, OpenRouterBackend, ZaiBackend
from analysis.llm.document_representation import render_documents
from analysis.llm.summarizer import Summarizer
from config import config, get_logger
from corpus.store import close_corpus, init_corpus
from database.db_postgres import Database
from pipeline.protocols import NullMetrics

logger = get_logger(__name__)

# OpenRouter provider slug to pin per model so a number measures the endpoint
# we would actually run on, not whichever mirror was cheapest that minute.
PROVIDER_PINS = {
    "z-ai/glm-5.3-flash": ("Z.AI",),
    "google/gemini-3.1-flash-lite": ("Google",),
    "google/gemini-2.5-flash-lite": ("Google",),
}

CANDIDATE_SQL = """
SELECT i.id, i.meeting_id, i.title, i.summary, i.topics, i.body_text,
       i.attachments, m.banana, m.date
FROM items i
JOIN meetings m ON m.id = i.meeting_id
WHERE i.summary IS NOT NULL AND i.summary <> ''
  AND i.prompts_version = $1
  AND i.summary_updated_at >= NOW() - ($2 || ' days')::interval
  AND jsonb_array_length(COALESCE(i.attachments, '[]'::jsonb)) BETWEEN 1 AND 6
ORDER BY random()
LIMIT $3
"""

SOURCE_SQL = """
SELECT ds.source_identity, db.content_sha256, db.text_key, db.page_count, db.text_chars
FROM document_source ds
JOIN document_blob db ON db.content_sha256 = ds.content_sha256
WHERE ds.source_identity = ANY($1::text[])
"""


async def rebuild_item_text(db, corpus, row, max_chars):
    """Rebuild the item's model input from corpus text, pipeline-style."""
    attachments = row["attachments"] or []
    if isinstance(attachments, str):
        attachments = json.loads(attachments)
    urls = [a.get("url") for a in attachments if isinstance(a, dict) and a.get("url")]
    if not urls:
        return None
    sources = {
        r["source_identity"]: r
        for r in await db.pool.fetch(SOURCE_SQL, urls)
    }
    doc_parts = []
    page_count = 0
    for att in attachments:
        url = att.get("url")
        source = sources.get(url)
        if not source or not source["text_key"]:
            return None
        extraction = await corpus.lookup_extraction(source["content_sha256"])
        if not extraction or not extraction.get("text"):
            return None
        doc_parts.append({"name": att.get("name") or url, "text": extraction["text"]})
        page_count += int(extraction.get("page_count") or 0)
    combined = render_documents(doc_parts)
    body_text = row["body_text"] or ""
    if body_text:
        combined += "\n\n=== Agenda item body text ===\n" + body_text
    if len(combined) > max_chars or len(combined) < 500:
        return None
    return combined, page_count or None


def build_bench_backend(spec):
    """"zai:glm-5.3-flash" and "anthropic:claude-haiku-5-5" hit native endpoints; anything else is an
    OpenRouter slug pinned to its known provider."""
    if spec.startswith("zai:"):
        return ZaiBackend(config.ZAI_API_KEY or "", spec.split(":", 1)[1])
    if spec.startswith("anthropic:"):
        return AnthropicBackend(config.ANTHROPIC_API_KEY or "", spec.split(":", 1)[1])
    return OpenRouterBackend(
        config.OPENROUTER_API_KEY or "",
        spec,
        provider_order=PROVIDER_PINS.get(spec, ()),
    )


def run_model(backend, prompts, title, text, page_count):
    """One call on a private Summarizer so the metrics tap sees only this call."""
    tap = CostTap()
    summarizer = Summarizer(metrics=tap, backend=backend)
    summarizer.prompts = prompts
    started = time.monotonic()
    try:
        summary, topics = summarizer.summarize_item(title, text, page_count)
        outcome = {
            "summary": summary,
            "topics": topics,
            "latency_s": round(time.monotonic() - started, 1),
            "error": None,
        }
    except Exception as exc:  # Comparison harness: record, never abort the run
        outcome = {
            "summary": None,
            "topics": [],
            "latency_s": round(time.monotonic() - started, 1),
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
    last = tap.last or {}
    outcome.update(
        {
            "input_tokens": last.get("input_tokens"),
            "output_tokens": last.get("output_tokens"),
            "cost_usd": last.get("cost_dollars"),
        }
    )
    return outcome


class CostTap(NullMetrics):
    """Metrics sink that keeps the last recorded call so the harness can
    attach tokens and cost to each row."""

    def __init__(self):
        super().__init__()
        self.last = None

    def record_llm_call(self, **kwargs):
        self.last = kwargs


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--prompts-version", default="v3.2")
    parser.add_argument("--models", default="z-ai/glm-5.3-flash,google/gemini-3.1-flash-lite")
    parser.add_argument("--effort", default="auto", help="auto (tiered) or low/medium/high")
    parser.add_argument("--max-chars", type=int, default=400_000)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--out", default="data/llm_compare")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    random.seed(args.seed)
    config.LLM_REASONING_EFFORT = args.effort
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    db = await Database.create()
    corpus = init_corpus(db.document_blobs)
    if corpus is None:
        raise SystemExit("corpus unavailable (R2 credentials or ENGAGIC_CORPUS_ENABLED)")

    backends = {m: build_bench_backend(m) for m in models}
    prompts = Summarizer(metrics=NullMetrics(), backend=backends[models[0]]).prompts

    try:
        candidates = await db.pool.fetch(CANDIDATE_SQL, args.prompts_version, str(args.days), args.limit * 6)
        selected = []
        seen_meetings = set()
        for row in candidates:
            if row["meeting_id"] in seen_meetings:
                continue
            rebuilt = await rebuild_item_text(db, corpus, row, args.max_chars)
            if rebuilt is None:
                continue
            text, page_count = rebuilt
            seen_meetings.add(row["meeting_id"])
            selected.append((row, text, page_count))
            if len(selected) >= args.limit:
                break
        print(f"selected {len(selected)} items from {len(candidates)} candidates")

        semaphore = asyncio.Semaphore(args.concurrency)
        results_path = out / "results.jsonl"
        results = []

        async def one(row, text, page_count):
            record = {
                "item_id": row["id"],
                "banana": row["banana"],
                "meeting_date": str(row["date"])[:10],
                "title": row["title"],
                "input_chars": len(text),
                "page_count": page_count,
                "stored": {"summary": row["summary"], "topics": row["topics"]},
                "models": {},
            }
            for model in models:
                async with semaphore:
                    outcome = await asyncio.to_thread(
                        run_model, backends[model], prompts, row["title"], text, page_count
                    )
                record["models"][model] = outcome
            results.append(record)
            with results_path.open("a") as handle:
                handle.write(json.dumps(record) + "\n")
            print(f"done {len(results)}/{len(selected)} {row['banana']} {row['title'][:60]}")

        results_path.write_text("")
        await asyncio.gather(*(one(*sel) for sel in selected))
        write_report(out / "report.md", results, models)
        print(f"wrote {results_path} and {out / 'report.md'}")
    finally:
        await close_corpus()
        await db.close()


def write_report(path, results, models):
    lines = [f"# Backend comparison: {len(results)} items", ""]
    lines.append("| model | ok | failed | avg out chars | avg cost $ | avg latency s | topic overlap w/ stored |")
    lines.append("|---|---|---|---|---|---|---|")
    for model in models:
        rows = [r["models"][model] for r in results]
        ok = [r for r in rows if r["summary"]]
        overlaps = []
        for r in results:
            stored = set(r["stored"]["topics"] or [])
            got = set(r["models"][model]["topics"] or [])
            if stored or got:
                overlaps.append(len(stored & got) / max(1, len(stored | got)))
        lines.append(
            f"| {model} | {len(ok)} | {len(rows) - len(ok)} | "
            f"{statistics.mean(len(r['summary']) for r in ok) if ok else 0:.0f} | "
            f"{statistics.mean(r['cost_usd'] or 0 for r in ok) if ok else 0:.4f} | "
            f"{statistics.mean(r['latency_s'] for r in ok) if ok else 0:.1f} | "
            f"{statistics.mean(overlaps) if overlaps else 0:.2f} |"
        )
    stored_lengths = [len(r["stored"]["summary"]) for r in results]
    lines.append(f"| stored (gemini) | {len(results)} | 0 | {statistics.mean(stored_lengths):.0f} | | | |")
    lines.append("")
    for index, r in enumerate(results, 1):
        lines.append(f"## {index}. {r['banana']} {r['meeting_date']}: {r['title']}")
        lines.append(f"input {r['input_chars']:,} chars, {r['page_count'] or '?'} pages, item `{r['item_id']}`")
        lines.append("")
        lines.append("### stored")
        lines.append(f"topics: {r['stored']['topics']}")
        lines.append("")
        lines.append(r["stored"]["summary"])
        lines.append("")
        for model in models:
            m = r["models"][model]
            lines.append(f"### {model}")
            if m["error"]:
                lines.append(f"ERROR: {m['error']}")
            else:
                lines.append(
                    f"topics: {m['topics']} | {m['input_tokens']} in / {m['output_tokens']} out"
                    f" | ${m['cost_usd'] or 0:.4f} | {m['latency_s']}s"
                )
                lines.append("")
                lines.append(m["summary"])
            lines.append("")
    path.write_text("\n".join(lines))


if __name__ == "__main__":
    asyncio.run(main())
