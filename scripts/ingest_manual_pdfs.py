"""Ingest manually-provided PDFs for cities behind bot protection (Akamai, etc.)

Reads PDFs from data/manual_pdfs/{banana}/, parses them with the agenda chunker,
stores meetings and items via the standard repositories, and enqueues for processing.

PDF filenames should encode meeting info:
  {MMDDYYYY}_{body}_{type}.pdf
  e.g. 03252026_Council_Agenda_Packet.pdf
       03042026_PC_Agenda_and_Packet.pdf

Or just drop any PDF and the script will extract date/title from the content.

Usage:
  uv run scripts/ingest_manual_pdfs.py portolavalleyCA
  uv run scripts/ingest_manual_pdfs.py portolavalleyCA --dir /path/to/pdfs
"""

import asyncio
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import fitz

from config import Config, get_logger
from database.db_postgres import Database
from database.id_generation import generate_item_id, generate_meeting_id
from database.models import AgendaItem, AttachmentInfo, Meeting
from pipeline.utils import meeting_work_version
from vendors.adapters.parsers.agenda_chunker import parse_agenda_pdf, _extract_memo_content

logger = get_logger(__name__).bind(component="manual_ingest")


async def _store_and_publish_meeting(
    db: Database,
    meeting: Meeting,
    agenda_items: List[AgendaItem],
    *,
    priority: int = 100,
) -> int:
    """Commit one authoritative manual snapshot and its outbox intent together."""
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            await db.meetings.store_meeting(meeting, conn=conn)
            stored = await db.items.store_agenda_items(
                meeting.id,
                agenda_items,
                conn=conn,
            )

            # Existing summarized items are freeze-on-summary, so the proposed
            # in-memory packet can differ from what the upsert retained. Re-read
            # the locked rows and publish exactly the descriptor now in the DB.
            authoritative_meeting = await db.meetings.get_meeting(
                meeting.id,
                conn=conn,
                lock_for_update=True,
            )
            if authoritative_meeting is None:  # pragma: no cover - same UoW insert
                raise RuntimeError(
                    f"manual meeting disappeared before publication: {meeting.id}"
                )
            authoritative_items = await db.items.get_agenda_items(
                meeting.id,
                conn=conn,
                lock_for_update=True,
            )
            work_version = meeting_work_version(
                authoritative_meeting,
                authoritative_items,
            )
            await db.pipeline_lifecycle.enqueue_queue_job(
                source_url=f"meeting://{meeting.id}",
                job_type="meeting",
                payload={"meeting_id": meeting.id},
                aggregate_id=meeting.id,
                meeting_id=meeting.id,
                banana=authoritative_meeting.banana,
                priority=priority,
                work_version=work_version,
                conn=conn,
            )
    return stored


def extract_date_from_filename(filename: str) -> Optional[datetime]:
    """Try to extract a date from the filename.

    Supports: MMDDYYYY, MM-DD-YYYY, YYYY-MM-DD patterns.
    """
    stem = Path(filename).stem

    # MMDDYYYY (e.g. 03252026)
    m = re.search(r'(\d{2})(\d{2})(\d{4})', stem)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass

    # YYYY-MM-DD
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', stem)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    return None


def extract_date_from_pdf(pdf_path: str) -> Optional[datetime]:
    """Extract meeting date from first page text."""
    doc = fitz.open(pdf_path)
    text = doc[0].get_text()[:1000]
    doc.close()

    # Common patterns: "March 25, 2026", "Wednesday, March 25, 2026"
    months = {
        'january': 1, 'february': 2, 'march': 3, 'april': 4,
        'may': 5, 'june': 6, 'july': 7, 'august': 8,
        'september': 9, 'october': 10, 'november': 11, 'december': 12,
    }
    m = re.search(
        r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?,?\s*'
        r'(January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+(\d{1,2}),?\s+(\d{4})',
        text, re.IGNORECASE
    )
    if m:
        month = months[m.group(1).lower()]
        day = int(m.group(2))
        year = int(m.group(3))
        try:
            return datetime(year, month, day)
        except ValueError:
            pass

    return None


def extract_title_from_pdf(pdf_path: str) -> str:
    """Extract meeting title from first page text."""
    doc = fitz.open(pdf_path)
    text = doc[0].get_text()[:500]
    doc.close()

    # Look for patterns like "Regular Meeting of the Town Council"
    # or "Planning Commission Meeting"
    patterns = [
        r'(?:Regular|Special|Joint)\s+Meeting\s+of\s+the\s+(.+?)(?:\n|Monday|Tuesday|Wednesday)',
        r'(.+?)\s+(?:Regular|Special)\s+Meeting',
        r'(.+?)\s+Meeting',
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            title = m.group(1).strip()
            title = re.sub(r'\s+', ' ', title)
            if len(title) > 5:
                return title

    return Path(pdf_path).stem


def parse_portola_valley_toc(doc, toc) -> Tuple[List[dict], List[Tuple[str, int, int]]]:
    """Parse Portola Valley-style TOC where L1 entries use 'Item X.y - Name' format.

    Returns (item_attachments_map, attachment_ranges) where:
    - item_attachments_map: {item_key: [(att_name, page_start, page_end), ...]}
    - attachment_ranges: all (name, start, end) tuples
    """
    item_attachments = defaultdict(list)
    all_att_pages = []

    for level, title, page in toc:
        m = re.match(r'Item\s+(\d+)\.([a-z])\s*-\s*(.*)', title)
        if m:
            item_key = f'{m.group(1)}.{m.group(2)}'
            att_name = m.group(3).strip()
            all_att_pages.append(page - 1)
            item_attachments[item_key].append((att_name, page - 1))

    if not all_att_pages:
        return {}, []

    all_att_pages_sorted = sorted(set(all_att_pages))

    # Compute page ranges
    result = defaultdict(list)
    for item_key, atts in item_attachments.items():
        for att_name, att_start in atts:
            idx = all_att_pages_sorted.index(att_start)
            if idx + 1 < len(all_att_pages_sorted):
                att_end = all_att_pages_sorted[idx + 1] - 1
            else:
                att_end = doc.page_count - 1
            result[item_key].append((att_name, att_start, att_end))

    return dict(result), []


def parse_items_from_agenda_pages(doc, agenda_end: int) -> List[dict]:
    """Parse numbered items from agenda pages using text extraction.

    Handles Portola Valley's format where numbers are in separate text spans:
      "1."  "CALL TO ORDER / ROLL CALL"
      "a."  "Sub-item title"
    """
    items = []
    current_num = None
    current_title = ''
    current_body_lines = []
    current_sub = None

    for p in range(agenda_end + 1):
        text = doc[p].get_text()
        lines = text.split('\n')

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            # Main item: standalone "1." "2." etc
            main_match = re.match(r'^(\d+)\.\s*$', stripped)
            if main_match:
                if current_num and current_title:
                    items.append({
                        'num': current_num,
                        'title': current_title.strip(),
                        'body': ' '.join(current_body_lines).strip(),
                        'sub': current_sub,
                    })
                current_num = main_match.group(1)
                current_title = ''
                current_body_lines = []
                current_sub = None
                continue

            # Sub-item: standalone "a." "b." etc
            sub_match = re.match(r'^([a-z])\.\s*$', stripped)
            if sub_match and current_num:
                if current_title:
                    items.append({
                        'num': current_num,
                        'title': current_title.strip(),
                        'body': ' '.join(current_body_lines).strip(),
                        'sub': current_sub,
                    })
                current_sub = sub_match.group(1)
                current_title = ''
                current_body_lines = []
                continue

            # Title or body text
            if current_num and not current_title:
                current_title = stripped
            elif current_num and current_title:
                current_body_lines.append(stripped)

    # Save last item
    if current_num and current_title:
        items.append({
            'num': current_num,
            'title': current_title.strip(),
            'body': ' '.join(current_body_lines).strip(),
            'sub': current_sub,
        })

    return items


def process_packet_pdf(pdf_path: str) -> Tuple[str, List[dict]]:
    """Process a packet PDF into meeting title + items with attachments.

    Tries multiple strategies:
    1. TOC with Item X.y pattern (Portola Valley-style)
    2. Standard agenda chunker
    3. Agenda page text parsing with TOC attachment mapping
    """
    doc = fitz.open(pdf_path)
    toc = doc.get_toc()

    # Check if TOC has Item X.y pattern
    has_item_toc = any(
        re.match(r'Item\s+\d+\.[a-z]\s*-', e[1]) for e in toc
    )

    title = extract_title_from_pdf(pdf_path)

    if has_item_toc:
        # Strategy 1: Parse agenda pages + map TOC attachments
        logger.info("using item-toc strategy", pdf=pdf_path)

        # Find where agenda ends (first Item entry)
        agenda_end = 2  # default
        for level, toc_title, page in toc:
            if toc_title.startswith('Item '):
                agenda_end = page - 2  # 0-indexed, one before
                break
        agenda_end = max(0, min(agenda_end, doc.page_count - 1))

        raw_items = parse_items_from_agenda_pages(doc, agenda_end)
        toc_attachments = parse_portola_valley_toc(doc, toc)[0]

        items = []
        for seq, item in enumerate(raw_items, 1):
            item_key = f'{item["num"]}.{item["sub"]}' if item['sub'] else item['num']

            body_parts = []
            if item['body']:
                body_parts.append(item['body'])

            attachment_list = []
            for att_name, att_start, att_end in toc_attachments.get(item_key, []):
                if 'Cover Page' in att_name:
                    cover_text = doc[att_start].get_text().strip()
                    if cover_text:
                        body_parts.append(cover_text)
                    continue

                memo = _extract_memo_content(doc, att_start, att_end)
                if memo.full_text:
                    body_parts.append(memo.full_text)

                attachment_list.append({
                    'name': att_name,
                    'url': '',
                    'type': 'embedded',
                })

            items.append({
                'vendor_item_id': item_key,
                'title': item['title'],
                'sequence': seq,
                'agenda_number': item_key,
                'body_text': '\n\n'.join(body_parts) if body_parts else None,
                'attachments': attachment_list if attachment_list else None,
            })

        doc.close()
        return title, items

    # Strategy 2: Standard chunker
    doc.close()
    logger.info("using standard chunker", pdf=pdf_path)
    parsed = parse_agenda_pdf(pdf_path)
    items = parsed.get('items', [])

    if items:
        return title, items

    # Strategy 3: Direct text parsing for thin agendas with split number/title spans
    logger.info("chunker returned 0 items, trying direct text parse", pdf=pdf_path)
    doc = fitz.open(pdf_path)
    agenda_end = min(doc.page_count - 1, 4)
    raw_items = parse_items_from_agenda_pages(doc, agenda_end)
    doc.close()

    if raw_items:
        return title, [
            {
                'vendor_item_id': f'{it["num"]}.{it["sub"]}' if it['sub'] else it['num'],
                'title': it['title'],
                'sequence': idx + 1,
                'agenda_number': f'{it["num"]}.{it["sub"]}' if it['sub'] else it['num'],
                'body_text': it['body'] if it['body'] else None,
                'attachments': None,
            }
            for idx, it in enumerate(raw_items)
        ]

    return title, []


async def ingest_city(banana: str, pdf_dir: str) -> None:
    """Ingest all PDFs in a directory for a given city."""
    db = await Database.create()

    pdf_files = sorted(Path(pdf_dir).glob('*.pdf'))
    if not pdf_files:
        logger.error("no PDFs found", dir=pdf_dir)
        await db.close()
        return

    logger.info("ingesting manual PDFs", banana=banana, count=len(pdf_files), dir=pdf_dir)

    meetings_stored = 0
    items_stored = 0

    for pdf_path in pdf_files:
        pdf_str = str(pdf_path)
        filename = pdf_path.name

        # Skip presentation/supplemental PDFs (handle separately if needed)
        if 'presentation' in filename.lower() and 'agenda' not in filename.lower():
            logger.info("skipping presentation PDF", file=filename)
            continue

        # Extract date
        meeting_date = extract_date_from_filename(filename)
        if not meeting_date:
            meeting_date = extract_date_from_pdf(pdf_str)
        if not meeting_date:
            logger.warning("could not extract date", file=filename)
            continue

        # Process PDF
        title, raw_items = process_packet_pdf(pdf_str)
        if not raw_items:
            logger.warning("no items extracted", file=filename, title=title)
            continue

        # Generate meeting ID
        vendor_id = f"manual_{pdf_path.stem}"
        meeting_id = generate_meeting_id(banana, vendor_id, meeting_date, title)

        # Build Meeting model
        meeting = Meeting(
            id=meeting_id,
            banana=banana,
            title=title,
            date=meeting_date,
            packet_url=pdf_str,
        )

        # Build AgendaItem models
        agenda_items = []
        for idx, item in enumerate(raw_items):
            seq = item.get('sequence', idx + 1)
            item_id = generate_item_id(
                meeting_id, seq, item.get('vendor_item_id')
            )

            attachments = None
            if item.get('attachments'):
                attachments = [
                    AttachmentInfo(
                        name=a.get('name', 'Attachment'),
                        url=a.get('url', ''),
                        type=a.get('type', 'embedded'),
                    )
                    for a in item['attachments']
                ]

            agenda_items.append(AgendaItem(
                id=item_id,
                meeting_id=meeting_id,
                title=item['title'],
                sequence=seq,
                agenda_number=item.get('agenda_number'),
                body_text=item.get('body_text'),
                attachments=attachments,
            ))

        stored = await _store_and_publish_meeting(
            db,
            meeting,
            agenda_items,
        )

        meetings_stored += 1
        items_stored += stored
        logger.info(
            "meeting ingested",
            meeting_id=meeting_id,
            title=title,
            date=meeting_date.strftime('%Y-%m-%d'),
            items=stored,
        )

    await db.close()
    logger.info(
        "ingestion complete",
        banana=banana,
        meetings=meetings_stored,
        items=items_stored,
    )


def main():
    if len(sys.argv) < 2:
        print("Usage: uv run scripts/ingest_manual_pdfs.py <banana> [--dir <pdf_dir>]")
        sys.exit(1)

    banana = sys.argv[1]

    if not re.match(r'^[a-zA-Z0-9-]+$', banana):
        print("Invalid banana identifier: must be alphanumeric/hyphens only")
        sys.exit(1)

    pdf_dir = None
    if '--dir' in sys.argv:
        idx = sys.argv.index('--dir')
        if idx + 1 < len(sys.argv):
            pdf_dir = sys.argv[idx + 1]

    if not pdf_dir:
        config = Config()
        pdf_dir = os.path.join(config.DB_DIR, 'manual_pdfs', banana)

    if not os.path.isdir(pdf_dir):
        print(f"Directory not found: {pdf_dir}")
        sys.exit(1)

    asyncio.run(ingest_city(banana, pdf_dir))


if __name__ == '__main__':
    main()
