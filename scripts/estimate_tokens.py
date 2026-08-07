"""Estimate total tokens across all stored attachment URLs via sampling.

Downloads a random sample of attachments, extracts text, measures character
counts, and extrapolates to the full population.

Usage: uv run scripts/estimate_tokens.py [--sample-size 100]
"""

import argparse
import asyncio
import os
import random
import statistics

import asyncpg
import fitz  # PyMuPDF
import aiohttp

# Rough token-per-character ratio for English text with typical LLM tokenizers
# GPT/Claude tokenizers average ~3.5-4.5 chars per token for English prose
CHARS_PER_TOKEN = 4.0

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/pdf,application/octet-stream,*/*",
}


async def get_random_urls(pool, sample_size):
    """Get random attachment URLs from items table, stratified by type."""
    rows = await pool.fetch("""
        WITH numbered AS (
            SELECT
                a->>'url' as url,
                a->>'type' as doc_type,
                a->>'name' as doc_name
            FROM items, jsonb_array_elements(COALESCE(attachments, '[]'::jsonb)) as a
            WHERE a->>'url' IS NOT NULL
            ORDER BY random()
            LIMIT $1
        )
        SELECT url, doc_type, doc_name FROM numbered
    """, sample_size * 3)  # overfetch to compensate for failures

    # Stratified: ~85% PDF, ~10% DOC, ~5% other (matching actual distribution)
    pdfs = [r for r in rows if r['doc_type'] == 'pdf']
    docs = [r for r in rows if r['doc_type'] == 'doc']
    others = [r for r in rows if r['doc_type'] not in ('pdf', 'doc')]

    pdf_n = int(sample_size * 0.85)
    doc_n = int(sample_size * 0.10)
    other_n = sample_size - pdf_n - doc_n

    sample = (
        random.sample(pdfs, min(pdf_n, len(pdfs)))
        + random.sample(docs, min(doc_n, len(docs)))
        + random.sample(others, min(other_n, len(others)))
    )
    return sample


async def download_and_extract(session, url, doc_type, timeout_secs=30):
    """Download a document and extract text. Returns (char_count, page_count, byte_size) or None."""
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=timeout_secs)) as resp:
            if resp.status != 200:
                return None
            raw = await resp.read()
            byte_size = len(raw)

            # Skip huge files (>50MB) to avoid memory issues
            if byte_size > 50 * 1024 * 1024:
                return None

            # Try to extract text
            try:
                doc = fitz.open(stream=raw, filetype="pdf" if doc_type == "pdf" else None)
                text = ""
                for page in doc:
                    text += page.get_text()
                page_count = len(doc)
                doc.close()
                return (len(text), page_count, byte_size)
            except Exception:
                # Fallback: just measure raw bytes, estimate 500 chars per KB for text-heavy docs
                return None
    except Exception:
        return None


async def get_population_counts(pool):
    """Get total attachment counts by type."""
    rows = await pool.fetch("""
        SELECT a->>'type' as doc_type, count(*) as cnt
        FROM items, jsonb_array_elements(COALESCE(attachments, '[]'::jsonb)) as a
        WHERE a->>'url' IS NOT NULL
        GROUP BY a->>'type'
    """)
    return {r['doc_type']: r['cnt'] for r in rows}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-size", type=int, default=150)
    parser.add_argument("--concurrency", type=int, default=10)
    args = parser.parse_args()

    dsn = f"postgresql://{os.environ.get('ENGAGIC_POSTGRES_USER', 'engagic')}:{os.environ.get('ENGAGIC_POSTGRES_PASSWORD', '')}@{os.environ.get('ENGAGIC_POSTGRES_HOST', 'localhost')}:{os.environ.get('ENGAGIC_POSTGRES_PORT', '5432')}/{os.environ.get('ENGAGIC_POSTGRES_DB', 'engagic')}"

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)

    print(f"Sampling {args.sample_size} attachment URLs...")
    sample = await get_random_urls(pool, args.sample_size)
    pop_counts = await get_population_counts(pool)
    total_attachments = sum(pop_counts.values())

    print(f"Population: {total_attachments:,} total attachments")
    for t, c in sorted(pop_counts.items(), key=lambda x: -x[1]):
        print(f"  {t}: {c:,}")
    print()

    # Download and extract
    sem = asyncio.Semaphore(args.concurrency)
    results = []
    failures = 0

    async with aiohttp.ClientSession() as session:
        async def process(row, idx):
            nonlocal failures
            async with sem:
                r = await download_and_extract(session, row['url'], row['doc_type'])
                if r is None:
                    failures += 1
                    if idx % 20 == 0:
                        print(f"  [{idx}/{len(sample)}] FAIL {row['url'][:80]}")
                else:
                    chars, pages, byte_size = r
                    results.append({
                        'chars': chars,
                        'pages': pages,
                        'bytes': byte_size,
                        'type': row['doc_type'],
                    })
                    if idx % 20 == 0:
                        print(f"  [{idx}/{len(sample)}] {chars:>8,} chars  {pages:>3} pages  {byte_size/1024:>8.1f} KB")

        tasks = [process(row, i) for i, row in enumerate(sample)]
        await asyncio.gather(*tasks)

    await pool.close()

    # Analysis
    success_count = len(results)
    print(f"\nResults: {success_count} succeeded, {failures} failed out of {len(sample)} sampled")

    if success_count < 10:
        print("Too few successful extractions to estimate. Aborting.")
        return

    success_rate = success_count / len(sample)

    char_counts = [r['chars'] for r in results]
    page_counts = [r['pages'] for r in results if r['pages'] > 0]
    byte_sizes = [r['bytes'] for r in results]

    sorted_chars = sorted(char_counts)
    mean_chars = statistics.mean(char_counts)
    median_chars = statistics.median(char_counts)
    stdev_chars = statistics.stdev(char_counts) if len(char_counts) > 1 else 0
    p10_chars = sorted_chars[len(sorted_chars) // 10]
    p25_chars = sorted_chars[len(sorted_chars) // 4]
    p75_chars = sorted_chars[3 * len(sorted_chars) // 4]
    p90_chars = sorted_chars[9 * len(sorted_chars) // 10]
    p99_chars = sorted_chars[int(len(sorted_chars) * 0.99)]
    max_chars = sorted_chars[-1]

    # Trimmed mean (drop top/bottom 5%) -- robust to fat tails
    trim_n = max(1, len(sorted_chars) // 20)
    trimmed = sorted_chars[trim_n:-trim_n]
    trimmed_mean = statistics.mean(trimmed)
    trimmed_stdev = statistics.stdev(trimmed) if len(trimmed) > 1 else 0

    # Winsorized mean (cap at P95 instead of removing)
    p95_chars = sorted_chars[int(len(sorted_chars) * 0.95)]
    winsorized = [min(c, p95_chars) for c in char_counts]
    winsorized_mean = statistics.mean(winsorized)

    mean_pages = statistics.mean(page_counts) if page_counts else 0
    mean_bytes = statistics.mean(byte_sizes)

    # Zero-char docs (scanned images without OCR, etc)
    zero_count = sum(1 for c in char_counts if c == 0)
    nonzero_chars = [c for c in char_counts if c > 0]
    nonzero_mean = statistics.mean(nonzero_chars) if nonzero_chars else 0
    nonzero_median = statistics.median(nonzero_chars) if nonzero_chars else 0

    accessible_docs = int(total_attachments * success_rate)

    print(f"\n{'='*60}")
    print(f"SAMPLE STATISTICS (n={success_count})")
    print(f"{'='*60}")
    print("  Characters per doc (all):")
    print(f"    Mean:          {mean_chars:>12,.0f}")
    print(f"    Trimmed mean:  {trimmed_mean:>12,.0f}  (drop top/bottom 5%)")
    print(f"    Winsorized:    {winsorized_mean:>12,.0f}  (cap at P95)")
    print(f"    Median:        {median_chars:>12,.0f}")
    print(f"    P10:           {p10_chars:>12,.0f}")
    print(f"    P25:           {p25_chars:>12,.0f}")
    print(f"    P75:           {p75_chars:>12,.0f}")
    print(f"    P90:           {p90_chars:>12,.0f}")
    print(f"    P99:           {p99_chars:>12,.0f}")
    print(f"    Max:           {max_chars:>12,.0f}")
    print(f"    StdDev:        {stdev_chars:>12,.0f}")
    print(f"  Zero-text docs:  {zero_count}/{success_count} ({zero_count/success_count*100:.1f}%)")
    print(f"  Non-zero mean:   {nonzero_mean:>12,.0f}")
    print(f"  Non-zero median: {nonzero_median:>12,.0f}")
    print(f"  Pages per doc:   {mean_pages:>8.1f} avg")
    print(f"  Bytes per doc:   {mean_bytes/1024:>8.1f} KB avg")
    print(f"  Success rate:    {success_rate*100:.1f}%")

    # Trimmed mean CI (more meaningful than raw mean CI for heavy tails)
    trim_se = trimmed_stdev / (len(trimmed) ** 0.5)
    ci_low_trim = (trimmed_mean - 1.96 * trim_se) * accessible_docs / CHARS_PER_TOKEN
    ci_high_trim = (trimmed_mean + 1.96 * trim_se) * accessible_docs / CHARS_PER_TOKEN

    total_tokens_trimmed = trimmed_mean * accessible_docs / CHARS_PER_TOKEN
    total_tokens_winsorized = winsorized_mean * accessible_docs / CHARS_PER_TOKEN
    total_tokens_median = median_chars * accessible_docs / CHARS_PER_TOKEN
    total_chars_mean = mean_chars * accessible_docs
    total_tokens_mean = total_chars_mean / CHARS_PER_TOKEN

    print(f"\n{'='*60}")
    print("EXTRAPOLATION TO FULL POPULATION")
    print(f"{'='*60}")
    print(f"  Total attachment URLs:        {total_attachments:>14,}")
    print(f"  Estimated accessible (95%):   {accessible_docs:>14,}")
    print()
    print(f"  Estimate (trimmed mean):      {total_tokens_trimmed:>14,.0f} tokens")
    print(f"  Estimate (winsorized mean):   {total_tokens_winsorized:>14,.0f} tokens")
    print(f"  Estimate (median):            {total_tokens_median:>14,.0f} tokens")
    print(f"  Estimate (raw mean):          {total_tokens_mean:>14,.0f} tokens")
    print(f"  95% CI (trimmed):             {ci_low_trim:>14,.0f} - {ci_high_trim:,.0f}")
    print()

    # Human-readable
    def human_tokens(n):
        if n >= 1e9:
            return f"{n/1e9:.1f}B"
        elif n >= 1e6:
            return f"{n/1e6:.1f}M"
        elif n >= 1e3:
            return f"{n/1e3:.1f}K"
        return str(int(n))

    print(f"  === {human_tokens(total_tokens_mean)} tokens (mean estimate) ===")
    print(f"  === {human_tokens(total_tokens_median)} tokens (median estimate) ===")
    print(
        f"  === 95% CI: {human_tokens(ci_low_trim)} - "
        f"{human_tokens(ci_high_trim)} ==="
    )

    # Also report stored text for comparison
    print(f"\n{'='*60}")
    print("FOR COMPARISON: STORED SUMMARIES/TEXT")
    print(f"{'='*60}")
    stored_chars = 59613213 + 61888624 + 35067610 + 6000747  # from earlier queries
    stored_tokens = stored_chars / CHARS_PER_TOKEN
    print(f"  Stored text (summaries+body): {stored_chars:>12,} chars = {human_tokens(stored_tokens)} tokens")
    print(f"  Ratio (source docs / stored): ~{total_chars_mean / stored_chars:.1f}x")


if __name__ == "__main__":
    asyncio.run(main())
