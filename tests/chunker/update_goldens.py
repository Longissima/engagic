"""Regenerate golden files from the fetched fixtures.

Run after intentional chunker changes, then review the golden diff like any
other code change — the diff IS the behavior change.

Usage:
    uv run python tests/chunker/update_goldens.py [meeting_id ...]
"""

import json
import sys

import corpus_lib


def main() -> None:
    only = set(sys.argv[1:])
    corpus_lib.GOLDEN_DIR.mkdir(exist_ok=True)
    for entry in corpus_lib.FETCHED:
        if only and entry["meeting_id"] not in only:
            continue
        result = corpus_lib.run_cached(entry)
        golden = corpus_lib.result_to_golden(entry, result)
        corpus_lib.golden_path(entry).write_text(json.dumps(golden, indent=2) + "\n")
        outcome = result.winning_rung or f"FAIL:{result.failure_reason}"
        print(f"{entry['vendor']:>14} {entry['meeting_id']:<45} {outcome:<14} "
              f"{len(result.items)} items")
    print(f"\ngoldens -> {corpus_lib.GOLDEN_DIR}")


if __name__ == "__main__":
    main()
