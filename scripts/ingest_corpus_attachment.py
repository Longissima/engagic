#!/usr/bin/env python3
"""Archive and extract one public attachment without resummarizing it."""

from __future__ import annotations

import argparse
import asyncio
import json

from analysis.analyzer_async import AsyncAnalyzer
from database.db_postgres import Database


async def ingest(url: str, banana: str | None) -> dict:
    db = await Database.create(min_size=1, max_size=2)
    analyzer = AsyncAnalyzer(enable_llm=False)
    try:
        result = await analyzer.extract_document_async(url, banana=banana)
        return {
            "content_sha256": result.get("content_sha256"),
            "corpus_persisted": result.get("corpus_persisted", False),
            "document_format": result.get("document_format"),
            "method": result.get("method"),
            "page_count": result.get("page_count"),
            "text_chars": len(result.get("text") or ""),
        }
    finally:
        await analyzer.close()
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--banana")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(ingest(args.url, args.banana)), indent=2))


if __name__ == "__main__":
    main()
