"""Backfill item_documents (migration 068) for every item that lists attachments.

Live writers keep the table current (item store, post-archival refresh, the
processor, the corpus window); this resolves the history once. It reads items
in id order and replaces each batch's rows through the same
refresh_item_documents the writers use. State is the table itself plus the
cursor printed every batch, so a restart is `--after <last id>`.

Disk is the constraint on this box: the run stops before free space falls
under --min-free-gb, leaving the cursor to resume from after space is freed.

    uv run python scripts/backfill_item_documents.py [--after ID] [--batch 2000] [--min-free-gb 2.5]
"""

import argparse
import asyncio
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import get_logger
from database.db_postgres import Database
from database.repositories_async.document_blobs import refresh_item_documents

logger = get_logger(__name__).bind(component="backfill_item_documents")

NEXT_IDS = """
    SELECT id FROM items
    WHERE id > $1 AND jsonb_typeof(attachments) = 'array' AND jsonb_array_length(attachments) > 0
    ORDER BY id LIMIT $2
"""


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--after", default="", help="resume after this item id")
    parser.add_argument("--batch", type=int, default=2000)
    parser.add_argument("--min-free-gb", type=float, default=2.5)
    args = parser.parse_args()

    db = await Database.create()
    cursor, items, resolved, started = args.after, 0, 0, time.monotonic()
    try:
        while True:
            free_gb = shutil.disk_usage("/").free / 1e9
            if free_gb < args.min_free_gb:
                logger.warning("stopping at disk floor", free_gb=round(free_gb, 2), resume_after=cursor)
                return 2
            async with db.pool.acquire() as conn:
                ids = [r["id"] for r in await conn.fetch(NEXT_IDS, cursor, args.batch)]
                if not ids:
                    break
                async with conn.transaction():
                    resolved += await refresh_item_documents(conn, ids, backdate=True)
            items += len(ids)
            cursor = ids[-1]
            logger.info("batch", items=items, resolved=resolved, resume_after=cursor,
                        free_gb=round(free_gb, 2), elapsed_s=int(time.monotonic() - started))
    finally:
        await db.close()
    logger.info("done", items=items, resolved=resolved, elapsed_s=int(time.monotonic() - started))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
