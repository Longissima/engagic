"""
Chunker health audit -- run on demand to spot-check item quality.

Usage:
    uv run scripts/audit_chunker.py                  # summary stats
    uv run scripts/audit_chunker.py --telemetry      # persisted path/filter/corpus audit
    uv run scripts/audit_chunker.py --sample 5       # pull 5 random meetings, show items
    uv run scripts/audit_chunker.py --method v2_toc  # filter to specific parse method
    uv run scripts/audit_chunker.py --flags          # show meetings with quality flags
"""

import argparse
import asyncio
import json

import asyncpg

from config import config


async def run_summary(conn, method_filter=None):
    """Print per-method health stats."""
    where_parts = ["m.processing_status = 'completed'"]
    params = []
    if method_filter:
        where_parts.append("m.processing_method = $1")
        params = [method_filter]
    where = " AND ".join(where_parts)

    rows = await conn.fetch(f"""
        SELECT
            m.processing_method,
            COUNT(DISTINCT m.id) as meetings,
            COUNT(DISTINCT m.id) FILTER (
                WHERE m.id IN (SELECT DISTINCT meeting_id FROM items)
            ) as has_items,
            ROUND(AVG(stats.item_ct)::numeric, 1) as avg_items,
            ROUND(AVG(stats.avg_body)::numeric, 0) as avg_body_len,
            SUM(stats.short_titles) as total_short_titles,
            SUM(stats.dup_titles) as total_dup_titles,
            SUM(stats.empty_body) as total_empty_body,
            SUM(stats.overlap) as total_overlap
        FROM meetings m
        LEFT JOIN LATERAL (
            SELECT
                COUNT(*) as item_ct,
                AVG(LENGTH(COALESCE(body_text, ''))) as avg_body,
                COUNT(*) FILTER (WHERE LENGTH(title) < 5) as short_titles,
                COUNT(*) - COUNT(DISTINCT title) as dup_titles,
                COUNT(*) FILTER (WHERE body_text IS NULL OR LENGTH(body_text) < 20) as empty_body,
                0 as overlap
            FROM items WHERE meeting_id = m.id
        ) stats ON true
        WHERE {where}
        GROUP BY m.processing_method
        ORDER BY COUNT(DISTINCT m.id) DESC
    """, *params)

    print(f"{'Method':<25} {'Mtgs':>5} {'w/Items':>7} {'Avg#':>5} {'AvgBody':>8} {'ShortT':>7} {'DupT':>5} {'NoBody':>7} {'Overlap':>8}")
    print("-" * 90)
    for r in rows:
        print(
            f"{r['processing_method'] or '?':<25} "
            f"{r['meetings']:>5} "
            f"{r['has_items']:>7} "
            f"{r['avg_items'] or 0:>5} "
            f"{r['avg_body_len'] or 0:>8.0f} "
            f"{r['total_short_titles'] or 0:>7} "
            f"{r['total_dup_titles'] or 0:>5} "
            f"{r['total_empty_body'] or 0:>7} "
            f"{r['total_overlap'] or 0:>8}"
        )


async def run_telemetry(conn):
    """Print the persisted processing audit in a machine-readable snapshot."""
    queries = {
        "chunk_paths": """
            SELECT COALESCE(run->>'ladder', '?') AS ladder,
                   COALESCE(run->>'winning_rung', 'none') AS winning_rung,
                   count(*) AS runs,
                   sum(COALESCE((run->>'item_count')::int, 0)) AS items
            FROM queue q
            CROSS JOIN LATERAL jsonb_array_elements(
                COALESCE(q.processing_metadata->'chunk'->'runs', '[]'::jsonb)
            ) AS runs(run)
            GROUP BY ladder, winning_rung
            ORDER BY runs DESC
        """,
        "chunk_smells": """
            SELECT COALESCE(
                       processing_metadata->'chunk'->'quality'->>'seg_smell',
                       'clean'
                   ) AS smell,
                   count(*) AS queue_rows
            FROM queue
            WHERE jsonb_typeof(
                processing_metadata->'chunk'->'quality'
            ) = 'object'
            GROUP BY smell
            ORDER BY queue_rows DESC
        """,
        "chunk_attempts": """
            SELECT attempt->>'rung' AS rung,
                   count(*) AS attempts,
                   count(*) FILTER (
                       WHERE COALESCE((attempt->>'items')::int, 0) > 0
                   ) AS successes,
                   round(avg(COALESCE((attempt->>'ms')::int, 0))) AS avg_ms,
                   max(COALESCE((attempt->>'ms')::int, 0)) AS max_ms
            FROM queue q
            CROSS JOIN LATERAL jsonb_array_elements(
                COALESCE(q.processing_metadata->'chunk'->'runs', '[]'::jsonb)
            ) AS runs(run)
            CROSS JOIN LATERAL jsonb_array_elements(
                COALESCE(run->'attempts', '[]'::jsonb)
            ) AS attempts(attempt)
            GROUP BY rung
            ORDER BY attempts DESC
        """,
        "ingest_paths": """
            SELECT source_path, count(*) AS observations,
                   sum(item_count) AS items,
                   sum(attachment_count) AS attachments,
                   sum(COALESCE(
                       (audit->'attachment_filter'->>'excluded')::int, 0
                   )) AS excluded_attachments
            FROM meeting_ingest_audits
            GROUP BY source_path
            ORDER BY observations DESC
        """,
        "current_filters": """
            SELECT filter_reason, filter_version, count(*) AS items
            FROM items
            WHERE filter_reason IS NOT NULL
            GROUP BY filter_reason, filter_version
            ORDER BY items DESC
        """,
        "filter_audits": """
            SELECT source, COALESCE(new_reason, 'eligible') AS verdict,
                   count(*) AS decisions
            FROM item_filter_audits
            GROUP BY source, verdict
            ORDER BY decisions DESC
        """,
        "extraction": """
            SELECT COALESCE(extraction_status, 'not_attempted') AS status,
                   count(*) AS documents
            FROM document_blob
            GROUP BY status
            ORDER BY documents DESC
        """,
    }
    snapshot = {
        name: [dict(row) for row in await conn.fetch(query)]
        for name, query in queries.items()
    }
    print(json.dumps(snapshot, indent=2, default=str, sort_keys=True))


async def run_flags(conn, method_filter=None, limit=20):
    """Show meetings with quality flags (short titles, dups, empty body)."""
    where_parts = ["m.processing_status = 'completed'"]
    params = []
    idx = 1

    if method_filter:
        where_parts.append(f"m.processing_method = ${idx}")
        params.append(method_filter)
        idx += 1

    where = " AND ".join(where_parts)

    rows = await conn.fetch(f"""
        SELECT
            m.banana, m.title as meeting_title, m.date,
            m.processing_method,
            stats.item_ct,
            stats.short_titles,
            stats.dup_titles,
            stats.empty_body,
            stats.avg_body::int as avg_body
        FROM meetings m
        JOIN LATERAL (
            SELECT
                COUNT(*) as item_ct,
                AVG(LENGTH(COALESCE(body_text, ''))) as avg_body,
                COUNT(*) FILTER (WHERE LENGTH(title) < 5) as short_titles,
                COUNT(*) - COUNT(DISTINCT title) as dup_titles,
                COUNT(*) FILTER (WHERE body_text IS NULL OR LENGTH(body_text) < 20) as empty_body
            FROM items WHERE meeting_id = m.id
        ) stats ON true
        WHERE {where}
            AND stats.item_ct > 0
            AND (stats.short_titles > 0 OR stats.dup_titles > 1 OR stats.empty_body > stats.item_ct * 0.5)
        ORDER BY stats.short_titles + stats.dup_titles + stats.empty_body DESC
        LIMIT ${idx}
    """, *params, limit)

    if not rows:
        print("No flagged meetings found.")
        return

    print(f"{'City':<22} {'Date':<12} {'Method':<20} {'Items':>5} {'Short':>5} {'Dups':>5} {'NoBody':>6} {'AvgBody':>8}")
    print("-" * 95)
    for r in rows:
        date_str = r['date'].strftime('%Y-%m-%d') if r['date'] else '?'
        print(
            f"{r['banana']:<22} "
            f"{date_str:<12} "
            f"{r['processing_method'] or '?':<20} "
            f"{r['item_ct']:>5} "
            f"{r['short_titles']:>5} "
            f"{r['dup_titles']:>5} "
            f"{r['empty_body']:>6} "
            f"{r['avg_body'] or 0:>8}"
        )


async def run_sample(conn, n=5, method_filter=None):
    """Pull N random meetings and show their items for manual inspection."""
    where_parts = [
        "m.processing_status = 'completed'",
        "m.id IN (SELECT DISTINCT meeting_id FROM items)",
    ]
    params = []
    idx = 1

    if method_filter:
        where_parts.append(f"m.processing_method = ${idx}")
        params.append(method_filter)
        idx += 1

    where = " AND ".join(where_parts)

    meetings = await conn.fetch(f"""
        SELECT m.id, m.banana, m.title, m.date, m.processing_method,
            (SELECT COUNT(*) FROM items WHERE meeting_id = m.id) as item_ct
        FROM meetings m
        WHERE {where}
        ORDER BY RANDOM()
        LIMIT ${idx}
    """, *params, n)

    for mtg in meetings:
        date_str = mtg['date'].strftime('%Y-%m-%d') if mtg['date'] else '?'
        print(f"\n{'=' * 70}")
        print(f"{mtg['banana']} | {date_str} | {mtg['processing_method']} | {mtg['item_ct']} items")
        print(f"{'=' * 70}")

        items = await conn.fetch("""
            SELECT agenda_number, title,
                LENGTH(COALESCE(body_text, '')) as body_len,
                attachments,
                LEFT(body_text, 120) as body_preview
            FROM items
            WHERE meeting_id = $1
            ORDER BY sequence
        """, mtg['id'])

        for item in items:
            att_ct = 0
            if item['attachments']:
                try:
                    import json
                    att_ct = len(json.loads(item['attachments'])) if isinstance(item['attachments'], str) else len(item['attachments'])
                except Exception:
                    pass

            num = item['agenda_number'] or '?'
            title = item['title'][:65] if item['title'] else '(no title)'
            body = item['body_len']
            preview = (item['body_preview'] or '').replace('\n', ' ')[:80]

            flag = ""
            if len(item['title'] or '') < 5:
                flag += " [SHORT-TITLE]"
            if body < 20:
                flag += " [NO-BODY]"

            print(f"  {num:<8} {title}")
            if body > 0:
                print(f"           body={body:,}ch  atts={att_ct}  {preview}...")
            else:
                print(f"           body=0  atts={att_ct}{flag}")


async def main():
    parser = argparse.ArgumentParser(description="Chunker health audit")
    parser.add_argument("--sample", type=int, default=0, help="Sample N random meetings for inspection")
    parser.add_argument("--method", type=str, default=None, help="Filter to specific parse method (e.g. v2_toc)")
    parser.add_argument("--flags", action="store_true", help="Show meetings with quality flags")
    parser.add_argument("--telemetry", action="store_true", help="Show persisted processing telemetry")
    parser.add_argument("--limit", type=int, default=20, help="Max rows for --flags")
    args = parser.parse_args()

    conn = await asyncpg.connect(config.get_postgres_dsn())

    try:
        if args.telemetry:
            await run_telemetry(conn)
        elif args.sample > 0:
            await run_sample(conn, n=args.sample, method_filter=args.method)
        elif args.flags:
            await run_flags(conn, method_filter=args.method, limit=args.limit)
        else:
            await run_summary(conn, method_filter=args.method)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
