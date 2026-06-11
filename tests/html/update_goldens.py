"""Regenerate HTML parser goldens. Review the diff — it IS the behavior change.

Usage:
    uv run python tests/html/update_goldens.py [meeting_id ...]
"""

import json
import sys

import html_corpus_lib as lib


def main() -> None:
    only = set(sys.argv[1:])
    lib.GOLDEN_DIR.mkdir(exist_ok=True)
    for entry in lib.FETCHED:
        if only and entry["meeting_id"] not in only:
            continue
        parsed = lib.run_cached(entry)
        golden = lib.result_to_golden(entry, parsed)
        lib.golden_path(entry).write_text(json.dumps(golden, indent=2) + "\n")
        print(f"{entry['vendor']:>10} {entry['meeting_id']:<42} "
              f"{golden['html_pattern'] or 'NO PATTERN':<28} {golden['item_count']} items")


if __name__ == "__main__":
    main()
