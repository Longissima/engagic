"""Archive historical originals and store meetings through the existing sync path.

Dates are [start, end). Completed intervals are subtracted on resume, including
overlapping requests. Discovery and per-meeting receipts survive interruption.
Meeting storage is enabled by default; --no-store-meetings keeps only originals
and manifests. Summary enqueueing is off unless --enqueue-summaries is supplied.
No OCR runs. Granicus agenda-only PDFs reuse v1 URL discovery for first-level
attachments; other PDF processing is deferred.
"""
import argparse
import asyncio
from collections import defaultdict, deque
from datetime import date, timedelta, datetime, timezone
import fcntl
from functools import partial
import json
from pathlib import Path
import sqlite3
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import config
from corpus.store import get_corpus
from database.db_postgres import Database
from pipeline.orchestrators.enqueue_decider import (
    SuppressedEnqueueDecider,
    SuppressedMatterEnqueueDecider,
)
from pipeline.orchestrators.meeting_sync import MeetingSyncOrchestrator
from pipeline.utils import canonical_fetch_url
from vendors.factory import get_async_adapter
from vendors.session_manager_async import AsyncSessionManager


def windows(start, end, completed):
    """Subtract completed coverage first, then split remaining spans by month."""
    spans = [(start, end)]
    for done_start, done_end in sorted(completed):
        remaining = []
        for left, right in spans:
            if done_end <= left or done_start >= right:
                remaining.append((left, right))
            else:
                if left < done_start:
                    remaining.append((left, done_start))
                if done_end < right:
                    remaining.append((done_end, right))
        spans = remaining
    for left, right in spans:
        while left < right:
            boundary = (left.replace(day=28) + timedelta(days=4)).replace(day=1)
            stop = min(boundary, right)
            yield left, stop
            left = stop


def emit(**values):
    print(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **values}, default=str), flush=True)


RETRY_DELAY_SECONDS = 6 * 3600
MAX_ATTEMPTS = 3


def initialize_checkpoint(state):
    state.execute("PRAGMA journal_mode=WAL")
    state.execute("""CREATE TABLE IF NOT EXISTS windows (
        source TEXT, start TEXT, end TEXT, status TEXT,
        meetings TEXT, receipts TEXT NOT NULL DEFAULT '{}', error TEXT,
        PRIMARY KEY(source,start,end))""")
    columns = {row[1] for row in state.execute("PRAGMA table_info(windows)")}
    for name, declaration in (
        ("discovery_error", "TEXT"), ("attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("next_retry_at", "REAL NOT NULL DEFAULT 0"),
    ):
        if name not in columns:
            state.execute(f"ALTER TABLE windows ADD COLUMN {name} {declaration}")
    state.execute("UPDATE windows SET attempts=1 WHERE status='failed' AND attempts=0")
    state.execute("""CREATE TABLE IF NOT EXISTS documents (
        banana TEXT, url TEXT, receipt TEXT NOT NULL, attempts INTEGER NOT NULL,
        next_retry_at REAL NOT NULL, run_id TEXT NOT NULL,
        PRIMARY KEY(banana,url))""")
    state.commit()


class ArchiveDocumentCheckpoint:
    def __init__(self, state):
        self.state = state
        self.run_id = uuid.uuid4().hex
        self.retry_failed = False

    def get(self, banana, url):
        row = self.state.execute(
            "SELECT receipt,attempts,next_retry_at,run_id FROM documents WHERE banana=? AND url=?",
            (banana, canonical_fetch_url(url))).fetchone()
        if row is None:
            return None
        receipt = json.loads(row[0])
        if receipt["status"] == "failed" and (
            self.retry_failed and row[1] < MAX_ATTEMPTS
            and row[2] <= time.time() and row[3] != self.run_id
        ):
            return None
        return {**receipt, "checkpoint_reused": True,
                "reused": receipt["status"] == "archived"}

    def record(self, banana, url, receipt):
        self.state.execute("""INSERT INTO documents
            (banana,url,receipt,attempts,next_retry_at,run_id) VALUES(?,?,?,1,?,?)
            ON CONFLICT(banana,url) DO UPDATE SET receipt=excluded.receipt,
            attempts=documents.attempts+1, next_retry_at=excluded.next_retry_at,
            run_id=excluded.run_id""",
            (banana, canonical_fetch_url(url), json.dumps(receipt),
             time.time() + RETRY_DELAY_SECONDS if receipt["status"] == "failed" else 0,
             self.run_id))
        self.state.commit()


def source_jobs(state, sources, start, end, phase, now):
    """Plan disjoint phases; rotate sources after each monthly window."""
    history = defaultdict(list)
    for source, left, right, status, attempts, retry_at in state.execute(
        "SELECT source,start,end,status,attempts,next_retry_at FROM windows"
    ):
        history[source].append((date.fromisoformat(left), date.fromisoformat(right),
                                status, attempts, retry_at))
    jobs = []
    for source, city in sorted(sources.items(), key=lambda entry: (
        bool(history.get(json.dumps(entry[0]))), entry[0]
    )):
        rows = history.get(json.dumps(source), [])
        if phase == "new":
            pending = list(windows(start, end, [(r[0], r[1]) for r in rows]))
        else:
            completed = [(r[0], r[1]) for r in rows if r[2] == "complete"]
            pending = []
            for left, right, status, attempts, retry_at in rows:
                eligible = status in {"running", "discovered"} if phase == "resume" else (
                    status == "failed" and attempts < MAX_ATTEMPTS and retry_at <= now)
                if eligible and left < end and right > start:
                    pending.extend(windows(max(left, start), min(right, end), completed))
        if pending:
            jobs.append((source, city, deque(sorted(set(pending)))))
    return jobs


async def execute_phase(state, sources, sync, args, semaphore, phase):
    queue = asyncio.Queue()
    for job in source_jobs(state, sources, args.start, args.end, phase, time.time()):
        queue.put_nowait(job)
    emit(event="phase_started", phase=phase, sources=queue.qsize())

    async def worker():
        while not queue.empty():
            source, city, pending = queue.get_nowait()
            if args.max_windows and args.processed_windows >= args.max_windows:
                return
            args.processed_windows += 1
            start, end = pending.popleft()
            identity = (json.dumps(source), start.isoformat(), end.isoformat())
            state.execute(
                "INSERT OR IGNORE INTO windows(source,start,end,status) VALUES(?,?,?,'running')", identity)
            state.execute(
                "UPDATE windows SET status='running',attempts=attempts+1 WHERE source=? AND start=? AND end=?", identity)
            state.commit()
            adapter = get_async_adapter(
                source[1], source[2],
                **({"api_token": config.NYC_LEGISTAR_TOKEN} if source[2] == "nyc" else {}))
            adapter.banana = city.banana
            try:
                await archive_source_window(state, identity, adapter, city, sync, args, semaphore)
            except Exception as exc:
                state.execute(
                    "UPDATE windows SET status='failed',error=?,next_retry_at=? WHERE source=? AND start=? AND end=?",
                    (str(exc), time.time() + RETRY_DELAY_SECONDS, *identity))
                state.commit()
                emit(event="window_failed", phase=phase, banana=city.banana,
                     start=start, end=end, error=str(exc))
            if pending:
                queue.put_nowait((source, city, pending))

    async with asyncio.TaskGroup() as group:
        for _ in range(args.source_concurrency):
            group.create_task(worker())


async def archive_window_meetings(
    meetings, receipts, *, archive_meeting, banana, state, identity, concurrency,
):
    """Overlap a bounded number of meetings; commit each finished receipt.

    Workers share an iterator, so a large window doesn't create a task per
    meeting. Receipt mutation and SQLite commit have no await between them;
    another worker cannot overwrite an out-of-date receipt snapshot.
    """
    pending = iter(enumerate(meetings))

    async def worker():
        for index, meeting in pending:
            prior = receipts.get(str(index))
            if prior and prior.get("success"):
                continue
            meeting_started = time.monotonic()
            receipt = await archive_meeting(meeting)
            receipts[str(index)] = receipt
            state.execute(
                "UPDATE windows SET receipts=? WHERE source=? AND start=? AND end=?",
                (json.dumps(receipts), *identity),
            )
            state.commit()
            emit(event="meeting_archived", banana=banana,
                 seconds=round(time.monotonic() - meeting_started, 2), **receipt)

    async with asyncio.TaskGroup() as group:
        for _ in range(min(concurrency, len(meetings))):
            group.create_task(worker())


async def archive_and_store(meeting_dict, *, sync, city, adapter, store_meetings, **archive_kwargs):
    """Archive a meeting's originals, then store it as an ordinary meeting.

    The adapter's meeting dict is exactly what sync_meeting takes, so storing
    it costs no vendor traffic. Skipping this step is what left 76k archived
    meetings with bytes in R2 and no row in Postgres; the manifest was their
    only record. A store failure must not fail the archival receipt: the
    bytes are the irreplaceable part, and the dict stays in the manifest for
    scripts/backfill_meetings_from_manifests.py to replay.
    """
    receipt = await sync.archive_meeting(meeting_dict, city=city, adapter=adapter, **archive_kwargs)
    if not store_meetings:
        return receipt
    try:
        meeting, stats = await sync.sync_meeting(meeting_dict, city)
        receipt["stored_meeting_id"] = meeting.id if meeting else None
        receipt["stored_items"] = stats.get("items_stored", 0)
    except Exception as exc:
        receipt["store_error"] = f"{type(exc).__name__}: {exc}"
        emit(event="meeting_store_failed", banana=city.banana,
             error=str(exc), error_type=type(exc).__name__)
    return receipt


async def archive_source_window(state, identity, adapter, city, sync, args, document_semaphore):
    window_started = time.monotonic()
    _, start, end = identity
    row = state.execute(
        "SELECT meetings,receipts,discovery_error FROM windows WHERE source=? AND start=? AND end=?",
        identity).fetchone()
    cached = bool(row and row[0] and not row[2] and not args.refresh_discovery)
    meetings = json.loads(row[0]) if cached else None
    receipts = json.loads(row[1]) if cached else {}
    discovery_error = None
    emit(event="window_started", banana=city.banana, slug=adapter.slug,
         start=start, end=end, discovery_cached=cached)
    state.execute(
        "INSERT OR IGNORE INTO windows(source,start,end,status) VALUES(?,?,?,'running')", identity)
    state.commit()
    if meetings is None:
        result = await adapter.fetch_meetings(start=start, end=end, originals_only=True)
        discovery_error = None if result.success else (result.error or "Incomplete discovery")
        if discovery_error and not result.meetings:
            raise RuntimeError(discovery_error)
        meetings = result.meetings
        emit(event="discovery_partial" if discovery_error else "discovery_complete",
             banana=city.banana, start=start, end=end,
             seconds=round(time.monotonic() - window_started, 2),
             meetings=len(meetings), error=discovery_error)
        # Partial results are durable but must be rediscovered on resume.
        # Receipt indexes reset with discovery; saved originals are reused by URL.
        state.execute(
            "UPDATE windows SET meetings=?,receipts='{}',status='discovered',error=NULL,discovery_error=? WHERE source=? AND start=? AND end=?",
            (json.dumps(meetings), discovery_error, *identity))
        state.commit()
    await archive_window_meetings(
        meetings, receipts, banana=city.banana,
        archive_meeting=partial(archive_and_store, sync=sync, city=city, adapter=adapter,
                                store_meetings=not args.no_store_meetings,
                                max_bytes=args.max_document_mib * 1024**2,
                                document_semaphore=document_semaphore,
                                document_checkpoint=getattr(args, "document_checkpoint", None)),
        state=state, identity=identity, concurrency=args.meeting_concurrency,
    )
    bad = sum(not r["success"] for r in receipts.values())
    if discovery_error or bad:
        raise RuntimeError(discovery_error or f"{bad} meetings have unarchived documents; retry this range")
    state.execute(
        "UPDATE windows SET status='complete',error=NULL WHERE source=? AND start=? AND end=?", identity)
    state.commit()
    emit(event="window_complete", banana=city.banana, start=start, end=end,
         seconds=round(time.monotonic() - window_started, 2),
         meetings=len(meetings), documents=sum(r["documents"] for r in receipts.values()),
         unavailable=sum(r.get("unavailable", 0) for r in receipts.values()),
         no_documents=sum(r["no_documents"] for r in receipts.values()))


async def run(args):
    db = await Database.create()
    try:
        cities = await db.jurisdictions.get_all_cities()
        wanted = set(args.banana.split(",")) if args.banana else None
        if wanted and wanted - {c.banana for c in cities}:
            raise ValueError(f"Unknown/inactive jurisdictions: {sorted(wanted - {c.banana for c in cities})}")
        sources = {}
        for city in cities:
            if wanted and city.banana not in wanted:
                continue
            for source in [{"vendor": city.vendor, "slug": city.slug}] + (city.extra_vendors or []):
                if source["vendor"] in args.vendors.split(","):
                    sources[(city.banana, source["vendor"], source["slug"])] = city
        if wanted and wanted - {key[0] for key in sources}:
            raise ValueError("Some selected jurisdictions have no source matching the selected vendors")
        emit(event="plan", sources=len(sources), start=args.start, end_exclusive=args.end,
             checkpoint=args.checkpoint, mode="originals_only" if args.no_store_meetings else "archive_and_store",
             source_concurrency=args.source_concurrency,
             meeting_concurrency=args.meeting_concurrency,
             document_concurrency=args.document_concurrency)
        if args.plan:
            return
        if not get_corpus():
            raise RuntimeError("Corpus is disabled; cannot start archival")

        checkpoint = Path(args.checkpoint)
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        with open(str(checkpoint) + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = sqlite3.connect(checkpoint)
            initialize_checkpoint(state)
            sync = MeetingSyncOrchestrator(db)
            if not args.enqueue_summaries:
                sync.enqueue_decider = SuppressedEnqueueDecider()
                sync.matter_enqueue_decider = SuppressedMatterEnqueueDecider()
            args.document_checkpoint = ArchiveDocumentCheckpoint(state)
            args.processed_windows = 0
            document_semaphore = asyncio.Semaphore(args.document_concurrency)
            try:
                for phase in ("new", "resume", "retry"):
                    args.document_checkpoint.retry_failed = phase == "retry"
                    await execute_phase(state, sources, sync, args, document_semaphore, phase)
                    if args.max_windows and args.processed_windows >= args.max_windows:
                        break
                failed = state.execute("SELECT count(*) FROM windows WHERE status='failed'").fetchone()[0]
                emit(event="finished", windows=args.processed_windows, deferred_windows=failed)
            finally:
                state.close()
    finally:
        await AsyncSessionManager.close_all()
        await db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat,
                        help="Exclusive end date")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--banana", help="Comma-separated jurisdiction IDs")
    selection.add_argument("--all", action="store_true", help="All active sources for the selected vendors")
    parser.add_argument("--vendors", default="legistar", help="Comma-separated: legistar,civicclerk,primegov,escribe,granicus,civicplus")
    parser.add_argument("--checkpoint", default="data/archive-meetings.sqlite3")
    parser.add_argument("--max-document-mib", type=int, default=2048)
    parser.add_argument("--source-concurrency", type=int, default=3)
    parser.add_argument("--meeting-concurrency", type=int, default=2, help="Concurrent meetings per source window")
    parser.add_argument("--document-concurrency", type=int, default=8, help="Global concurrent original downloads/uploads")
    parser.add_argument("--max-windows", type=int, default=0, help="Stop after this many windows; zero means all")
    parser.add_argument("--refresh-discovery", action="store_true", help="Re-fetch unfinished windows; completed intervals and archived originals are still skipped")
    parser.add_argument("--plan", action="store_true", help="Show source count without writing or fetching documents")
    parser.add_argument("--no-store-meetings", action="store_true",
                        help="Archive originals only, leaving no meeting rows. The old "
                             "behaviour; needs a manifest replay afterwards to be reachable")
    parser.add_argument("--enqueue-summaries", action="store_true",
                        help="Let archived meetings buy LLM summaries; off by default")
    args = parser.parse_args()
    if set(args.vendors.split(",")) - {"legistar", "civicclerk", "primegov", "escribe", "granicus", "civicplus"}:
        parser.error("Only legistar, civicclerk, primegov, escribe, granicus and civicplus have verified originals mode")
    if args.start >= args.end or args.max_document_mib <= 0 or args.max_windows < 0 or args.source_concurrency < 1 or args.document_concurrency < 1 or args.meeting_concurrency < 1:
        parser.error("Use increasing dates and positive document allowance")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
