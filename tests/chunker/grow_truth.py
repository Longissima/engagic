"""Grow the ground-truth corpus by having Gemini read fetched fixtures.

The extraction-quality loop, automated: a fixture without a truth file gets
its front pages read by Gemini (rendered PDF -> substantive item list),
the chunker's current output is scored against that reading, and the truth
file is written with expected_recall/expected_precision pinned at the
measured values. From then on test_ground_truth ratchets: chunker changes
may only move the pins up.

Review the new truth/*.json like any golden diff — the LLM reading is a
draft of reality, not reality. Spot-check titles against the fixture
before trusting a pin.

Closing the loop end to end: pull failing/smelly meetings from the prod
audit pool into tests/chunker_corpus_seed.tsv (see README), run
fetch_fixtures.py, then this. Costs pennies per document on flash-lite.

Usage:
    GEMINI_API_KEY=... uv run python tests/chunker/grow_truth.py            # all fixtures lacking truth
    GEMINI_API_KEY=... uv run python tests/chunker/grow_truth.py baytownTX_b02648a4
    ... grow_truth.py --limit 5 --max-pages 8 --model gemini-3.1-flash-lite
"""

import argparse
import json
import math
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

import fitz

import corpus_lib

TRUTH_DIR = Path(__file__).parent / "truth"

TRUTH_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "items": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "number": {"type": "STRING", "description": "agenda numbering as printed, e.g. 4.B"},
                    "title": {"type": "STRING", "description": "the item's title/subject as printed"},
                },
                "required": ["number", "title"],
            },
        },
        "notes": {"type": "STRING", "description": "anything odd about the document's structure"},
    },
    "required": ["items"],
}

PROMPT = """Read this city meeting agenda and list every substantive agenda item.

Substantive = anything a resident could care about: motions, ordinances,
resolutions, contracts, hearings, appointments, staff reports, presentations,
budget actions, closed-session matters.

Skip pure procedure: call to order, roll call, pledge of allegiance,
approval of the agenda itself, public comment placeholders, adjournment.

Use the document's own numbering and wording. If the document is a packet
(agenda followed by attachments), read only the agenda portion."""


def _slice_pdf(src: Path, max_pages: int) -> tuple[str, int, bool]:
    """Return (path_to_upload, pages_read, sliced). Slices long packets so a
    300-page packet costs front-pages, not the whole document."""
    doc = fitz.open(str(src))
    try:
        total = doc.page_count
        if total <= max_pages:
            return str(src), total, False
        out = fitz.open()
        out.insert_pdf(doc, to_page=max_pages - 1)
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        out.save(tmp.name)
        out.close()
        return tmp.name, max_pages, True
    finally:
        doc.close()


def read_truth(client, model: str, pdf_path: str, pages_read: int, sliced: bool) -> dict:
    from google.genai import types

    uploaded = client.files.upload(file=pdf_path)
    try:
        response = client.models.generate_content(
            model=model,
            contents=[uploaded, PROMPT],
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
                response_schema=TRUTH_SCHEMA,
            ),
        )
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass

    parsed = json.loads(response.text or "{}")
    notes = parsed.get("notes") or ""
    if sliced:
        partial = (f"PARTIAL truth: read pages 1-{pages_read} only; "
                   "recall is against the verified subset.")
        notes = f"{partial} {notes}".strip()
    return {
        "read_by": f"{model}, from PDF pages 1-{pages_read}, {date.today().isoformat()}",
        "notes": notes,
        "items": parsed.get("items") or [],
    }


def pin(value: float) -> float:
    """Floor to 2dp — pins must be reachable by the run that set them."""
    return math.floor(value * 100) / 100


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM-read fixtures into ground-truth files")
    parser.add_argument("meeting_ids", nargs="*", help="specific fixtures (default: all lacking truth)")
    parser.add_argument("--limit", type=int, default=10, help="max new truth files per run")
    parser.add_argument("--max-pages", type=int, default=8, help="read at most this many front pages")
    parser.add_argument("--model", default=os.getenv("ENGAGIC_PRIMARY_MODEL", "gemini-3.1-flash-lite"))
    args = parser.parse_args()

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("LLM_API_KEY")
    if not api_key:
        sys.exit("GEMINI_API_KEY (or LLM_API_KEY) required — run where the key lives")

    from google import genai
    client = genai.Client(api_key=api_key)

    wanted = set(args.meeting_ids)
    if wanted:
        entries = [e for e in corpus_lib.FETCHED if e["meeting_id"] in wanted]
    else:
        entries = [
            e for e in corpus_lib.FETCHED
            if not (TRUTH_DIR / f"{e['meeting_id']}.json").exists()
        ][: args.limit]
    if not entries:
        print("nothing to do: every fetched fixture already has a truth file")
        return

    TRUTH_DIR.mkdir(exist_ok=True)
    for entry in entries:
        mid = entry["meeting_id"]
        fixture = corpus_lib.FIXTURES_DIR / entry["filename"]
        upload_path, pages_read, sliced = _slice_pdf(fixture, args.max_pages)
        try:
            truth = read_truth(client, args.model, upload_path, pages_read, sliced)
        except Exception as e:
            print(f"{mid:<45} READ FAILED: {type(e).__name__}: {e}")
            continue
        finally:
            if sliced:
                os.unlink(upload_path)

        if not truth["items"]:
            print(f"{mid:<45} 0 items read — not writing a truth file")
            continue

        result = corpus_lib.run_cached(entry)
        recall, precision = corpus_lib.score_extraction(truth["items"], result.items)
        truth["expected_recall"] = pin(recall)
        truth["expected_precision"] = pin(precision)

        (TRUTH_DIR / f"{mid}.json").write_text(json.dumps(truth, indent=2) + "\n")
        print(f"{mid:<45} {len(truth['items'])} true items | "
              f"recall {recall:.2f} precision {precision:.2f} -> pinned")

    print(f"\ntruth files -> {TRUTH_DIR}\nreview the diff, then: uv run pytest tests/chunker/test_ground_truth.py")


if __name__ == "__main__":
    main()
