from types import SimpleNamespace
from typing import cast

import pytest

from database.db_postgres import Database
from pipeline.reconciliation import ReconciliationAction, plan_matter_reconciliation
from pipeline.utils import matter_attachment_version, matter_work_version
from scripts import reconcile_matter_queue


def appearances(summary=None):
    return [
        SimpleNamespace(
            id="item-1",
            meeting_id="meeting-1",
            sequence=1,
            title="Matter title",
            attachments=[
                SimpleNamespace(
                    name="Staff report",
                    url="https://example.test/staff-report.pdf",
                    type="application/pdf",
                )
            ],
            summary=summary,
        )
    ]


def test_missing_or_legacy_queue_descriptor_gets_current_version():
    items = appearances()
    missing = plan_matter_reconciliation(
        matter_id="alphaCA_matter",
        appearances=items,
        queue_row=None,
        canonical_summary=None,
        canonical_attachment_hash=None,
    )
    legacy = plan_matter_reconciliation(
        matter_id="alphaCA_matter",
        appearances=items,
        queue_row={"status": "completed", "work_version": None},
        canonical_summary=None,
        canonical_attachment_hash=None,
    )

    assert missing.action is ReconciliationAction.ENQUEUE_VERSION
    assert legacy.action is ReconciliationAction.ENQUEUE_VERSION
    assert missing.desired_version == matter_work_version(items)


def test_terminal_current_version_reactivates_only_when_projection_incomplete():
    items = appearances(summary="snapshot")
    version = matter_work_version(items)
    attachment_version = matter_attachment_version(items)

    incomplete = plan_matter_reconciliation(
        matter_id="alphaCA_matter",
        appearances=items,
        queue_row={"status": "completed", "work_version": version},
        canonical_summary=None,
        canonical_attachment_hash=attachment_version,
        canonical_work_version=version,
    )
    current = plan_matter_reconciliation(
        matter_id="alphaCA_matter",
        appearances=items,
        queue_row={"status": "completed", "work_version": version},
        canonical_summary="canonical",
        canonical_attachment_hash=attachment_version,
        canonical_work_version=version,
    )

    assert incomplete.action is ReconciliationAction.REACTIVATE_VERSION
    assert current.action is ReconciliationAction.NONE


@pytest.mark.asyncio
async def test_execute_reconciliation_uses_canonical_queue_lock_order() -> None:
    events: list[str] = []
    current_appearances = appearances()

    class Transaction:
        async def __aenter__(self):
            events.append("begin")

        async def __aexit__(self, exc_type, *_args):
            events.append("rollback" if exc_type else "commit")
            return False

    class Connection:
        def transaction(self):
            return Transaction()

        async def fetchrow(self, *_args, **_kwargs):
            raise AssertionError("reconciliation must not lock queue rows directly")

    connection = Connection()

    class Acquire:
        async def __aenter__(self):
            return connection

        async def __aexit__(self, *_args):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    class Matters:
        async def get_matter(self, matter_id, *, conn, lock_for_update):
            assert (matter_id, conn, lock_for_update) == (
                "alphaCA_matter",
                connection,
                True,
            )
            events.append("lock matter")
            return SimpleNamespace(
                id=matter_id,
                banana="alphaCA",
                canonical_summary=None,
                metadata=None,
                title="Matter title",
            )

    class Lifecycle:
        async def has_unresolved_outbox_for_aggregate(self, **kwargs):
            assert kwargs["conn"] is connection
            events.append("check outbox")
            return False

    class Items:
        async def get_all_items_for_matter(
            self, matter_id, *, conn, lock_for_update
        ):
            assert (matter_id, conn, lock_for_update) == (
                "alphaCA_matter",
                connection,
                True,
            )
            events.append("lock items")
            return current_appearances

    class Queue:
        async def lock_desired_state(self, source_url, *, conn):
            assert (source_url, conn) == (
                "matter://alphaCA_matter",
                connection,
            )
            events.append("lock queue source then row")
            return None

        async def enqueue_job(self, *, source_url, conn, **kwargs):
            assert source_url == "matter://alphaCA_matter"
            assert conn is connection
            assert kwargs["work_version"] == matter_work_version(
                current_appearances
            )
            events.append("enqueue")
            return True

    db = SimpleNamespace(
        pool=Pool(),
        matters=Matters(),
        pipeline_lifecycle=Lifecycle(),
        items=Items(),
        queue=Queue(),
    )

    plan = await reconcile_matter_queue._apply_current_plan(
        cast(Database, db),
        "alphaCA_matter",
    )

    assert plan is not None
    assert plan.action is ReconciliationAction.ENQUEUE_VERSION
    assert events == [
        "begin",
        "lock matter",
        "check outbox",
        "lock items",
        "lock queue source then row",
        "enqueue",
        "commit",
    ]
