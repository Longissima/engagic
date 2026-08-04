"""Compatibility entry point for the renamed v3.2 summary backfill.

Use ``scripts/backfill_v32_summaries.py`` for new invocations.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.backfill_v32_summaries import main


if __name__ == "__main__":
    asyncio.run(main())
