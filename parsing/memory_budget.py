"""Process-wide reservations shared by download tasks and extraction threads.

The fixed budget bounds admitted work even before allocations become visible
to the kernel. MemAvailable supplies an additional, conservative pressure
check; it never replaces accounting for outstanding reservations.
"""

import asyncio
from collections import deque
import threading
import time
from typing import Optional

from config import config

_MEMINFO = "/proc/meminfo"
_POLL_SECONDS = 0.1
_lock = threading.Lock()
_reserved_bytes = 0
# (ticket, size) so the head's requirement is known to every other waiter;
# conservative backfill needs it to prove a grant cannot delay the head.
_waiters: deque[tuple] = deque()


class MemoryAdmissionTimeout(TimeoutError):
    """No capacity became available; the expensive operation must not start."""


class MemoryAdmissionCancelled(Exception):
    """The owning async task cancelled while its thread was waiting."""


def available_bytes() -> Optional[int]:
    """MemAvailable from the kernel, or None where /proc is unavailable."""
    try:
        with open(_MEMINFO) as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


class MemoryReservation:
    """Capacity held until the operation (or its retained bytes) is released."""

    def __init__(self, size: int):
        self.size = size

    def release(self) -> None:
        global _reserved_bytes
        with _lock:
            _reserved_bytes -= self.size
            self.size = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()

    def __del__(self):
        self.release()


class ReservedBytes(bytes):
    """Keep a download's reservation while any consumer retains its bytes.

    Copying into this bytes subclass temporarily doubles the buffer; download
    reservations account for that copy. Existing bytes consumers work
    unchanged, including the compatibility byte-download API.
    """

    reservation: MemoryReservation

    def __new__(cls, data: bytes, reservation: MemoryReservation):
        value = super().__new__(cls, data)
        value.reservation = reservation
        return value


def reservable_bytes() -> int:
    """Largest reservation that could be granted right now, budget and host.

    Callers that choose their own work use this to pick something that fits
    instead of attempting the largest item and retrying on refusal. That keeps
    the admission gate a safety net rather than a scheduler: a worker holding a
    500 MB document should not discover the box is full only after paying for
    the download.

    Advisory only -- it is a snapshot under the same lock the grant path uses,
    and a concurrent reservation can consume the headroom before the caller
    acts. reserve_memory remains the authority.
    """
    with _lock:
        by_budget = config.WORK_MEMORY_BUDGET_BYTES - _reserved_bytes
        available = available_bytes()
        if available is None:
            return max(0, by_budget)
        by_host = available - config.EXTRACTION_MIN_AVAILABLE_BYTES - _reserved_bytes
        return max(0, min(by_budget, by_host))


def _enqueue(size: int) -> object:
    if size <= 0:
        raise ValueError("Memory reservation must be positive")
    if size > config.WORK_MEMORY_BUDGET_BYTES:
        raise MemoryAdmissionTimeout("Operation exceeds the shared memory budget")
    ticket = object()
    with _lock:
        _waiters.append((ticket, size))
    return ticket


def _leave_queue(ticket: object) -> None:
    with _lock:
        for entry in _waiters:
            if entry[0] is ticket:
                _waiters.remove(entry)
                break


def _try_reserve(size: int, ticket: object) -> Optional[MemoryReservation]:
    """Admit the head, or backfill behind it when that provably costs it nothing.

    Strict FIFO protects a large extraction from a stream of small downloads,
    but it also means one oversized document at the head stalls everything
    behind it. Measured 2026-09-20: Los Angeles ships 511 MB PrimeGov bundles
    that reserve 1,021 MB each, two workers grabbed two at once, together they
    exceeded the 2,048 MB budget, and the queue did zero extractions per hour
    while both timed out and requeued into the same collision.

    A non-head waiter may reserve only when the head's own requirement still
    fits the budget afterwards:

        _reserved_bytes + size + head_size <= WORK_MEMORY_BUDGET_BYTES

    That invariant is what makes this safe rather than merely faster. Because
    it holds after every grant, the head's budget test can never fail, so the
    head is admitted the moment it is checked and waits only on real host
    memory. Small work flows past a stalled giant; the giant cannot starve.

    Confidence: 9/10 - the budget half is a proof, not a heuristic. The
    MemAvailable half stays conservative and is re-read per attempt.
    """
    global _reserved_bytes
    with _lock:
        if not _waiters:
            return None
        head_ticket, head_size = _waiters[0]
        is_head = head_ticket is ticket
        available = available_bytes()
        required = _reserved_bytes + size
        if required > config.WORK_MEMORY_BUDGET_BYTES:
            return None
        if not is_head and required + head_size > config.WORK_MEMORY_BUDGET_BYTES:
            return None
        if available is not None and available < required + config.EXTRACTION_MIN_AVAILABLE_BYTES:
            return None
        reservation = MemoryReservation(size)
        _reserved_bytes += size
        if is_head:
            _waiters.popleft()
        else:
            for entry in _waiters:
                if entry[0] is ticket:
                    _waiters.remove(entry)
                    break
        return reservation


def reserve_memory(
    size: int,
    *,
    deadline: Optional[float] = None,
    cancel_event: Optional[threading.Event] = None,
) -> MemoryReservation:
    """Reserve atomically, or fail closed at the admission/operation deadline."""
    admission_deadline = time.monotonic() + config.EXTRACTION_MEMORY_WAIT_SECONDS
    if deadline is not None:
        admission_deadline = min(deadline, admission_deadline)
    ticket = _enqueue(size)
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise MemoryAdmissionCancelled("Memory admission cancelled")
            if time.monotonic() >= admission_deadline:
                raise MemoryAdmissionTimeout("Timed out waiting for shared memory capacity")
            reservation = _try_reserve(size, ticket)
            if reservation is not None:
                return reservation
            delay = min(_POLL_SECONDS, max(0, admission_deadline - time.monotonic()))
            if cancel_event is not None:
                cancel_event.wait(delay)
            else:
                time.sleep(delay)
    finally:
        _leave_queue(ticket)


async def reserve_memory_async(size: int) -> MemoryReservation:
    """Use the same accounting without occupying an executor thread."""
    deadline = time.monotonic() + config.EXTRACTION_MEMORY_WAIT_SECONDS
    ticket = _enqueue(size)
    try:
        while True:
            if time.monotonic() >= deadline:
                raise MemoryAdmissionTimeout("Timed out waiting for shared memory capacity")
            reservation = _try_reserve(size, ticket)
            if reservation is not None:
                return reservation
            await asyncio.sleep(min(_POLL_SECONDS, max(0, deadline - time.monotonic())))
    finally:
        _leave_queue(ticket)
