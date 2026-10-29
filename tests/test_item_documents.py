"""item_documents joins an item's stored attachments to the corpus (migration 068)."""

import asyncio

from database.repositories_async import document_blobs as blobs
from pipeline.utils import attachment_source_identities

SHA_A, SHA_B = "a" * 64, "b" * 64


class _Conn:
    """Answers exactly refresh_item_documents' statements over in-memory rows."""

    def __init__(self, items: dict, sources: dict):
        self.items, self.sources = items, sources
        self.inserted: list[tuple] = []
        self.deleted_for: list[str] = []

    async def fetch(self, sql, values):
        if sql is blobs._ITEM_ATTACHMENTS_SQL:
            return [{"id": i, "attachments": self.items[i]} for i in values if i in self.items]
        assert sql is blobs._IDENTITY_SHAS_SQL
        return [{"source_identity": k, "content_sha256": self.sources[k]} for k in values if k in self.sources]

    async def execute(self, sql, *cols):
        if sql is blobs._ITEM_DOCUMENTS_DELETE_SQL:
            self.deleted_for = cols[0]
        else:
            assert sql is blobs._ITEM_DOCUMENTS_INSERT_SQL
            self.inserted = list(zip(*cols[:5]))


def _refresh(items, sources, ids):
    conn = _Conn(items, sources)
    n = asyncio.run(blobs.refresh_item_documents(conn, ids))
    return n, conn


def test_each_resolved_attachment_becomes_a_row_with_its_position_and_span():
    items = {"i1": [{"url": "https://h/staff.pdf"},
                    {"url": "https://h/packet.pdf", "pages": [4, 9]},
                    {"url": "https://h/not-archived.pdf"}]}
    n, conn = _refresh(items, {"https://h/staff.pdf": SHA_A, "https://h/packet.pdf": SHA_B}, ["i1"])
    assert n == 2
    assert conn.inserted == [(bytes.fromhex(SHA_A), "i1", 0, None, None), (bytes.fromhex(SHA_B), "i1", 1, 4, 9)]
    assert conn.deleted_for == ["i1"]


def test_a_signed_url_resolves_through_its_stable_identity():
    items = {"i1": [{"url": "https://x.blob.core.windows.net/c/a.pdf?sv=2021&sig=SECRET"}]}
    n, _ = _refresh(items, {"https://x.blob.core.windows.net/c/a.pdf": SHA_A}, ["i1"])
    assert n == 1


def test_onbase_falls_back_to_the_sibling_endpoint_the_corpus_archived():
    view = "https://h/OnBaseAgendaOnline/Documents/ViewDocument/r.pdf?meetingId=1"
    n, _ = _refresh({"i1": [{"url": view}]},
                    {view.replace("ViewDocument", "DownloadFileBytes"): SHA_A}, ["i1"])
    assert n == 1


def test_an_item_with_nothing_archived_still_clears_its_old_rows():
    n, conn = _refresh({"i1": [{"url": "https://h/gone.pdf"}]}, {}, ["i1"])
    assert n == 0 and conn.inserted == [] and conn.deleted_for == ["i1"]


def test_civicclerk_portal_attachment_names_the_permanent_api_url_first():
    ids = attachment_source_identities({
        "url": "https://x.blob.core.windows.net/a.pdf?sv=1&sig=z",
        "portal_url": "https://acme.portal.civicclerk.com/event/9/files/attachment/77"})
    assert ids == ["https://acme.api.civicclerk.com/v1/Meetings/GetAttachmentFile(fileId=77)",
                   "https://x.blob.core.windows.net/a.pdf"]
