"""Subprocess guard for crash-prone, memory-hungry CPU work.

PyMuPDF is a C extension that can segfault on malformed PDFs, and parse/OCR
work on pathological documents can eat unbounded RAM or wedge forever (the
2026-06-29 sync freeze: one unguarded get_text pinned the whole pipeline for
15 minutes -- max_ms=902907 is still visible in the chunk audit telemetry).
Work like that must run where a crash, an OOM, or a hang is contained: an
isolated child process with a hard address-space cap and a kill-on-timeout
parent.

This module is the single implementation of that containment -- the collapse
chokepoint from docs/CORPUS_ARCHITECTURE.md. It was extracted from
analyzer_async's _extract_pdf_in_subprocess, previously the only guarded
site while the sync chunker ran naked in the event loop's thread pool (see
docs/DEBT_CLASS_RADAR.md item 2, "siloed" flavor). Both extraction
(analysis/analyzer_async.py) and the sync chunker
(vendors/adapters/base_adapter_async.py) now dispatch through it.

run_guarded() blocks; call it via asyncio.to_thread from async code.
"""

import multiprocessing
import resource
import time
from queue import Empty
from typing import Any, Callable, Dict, Optional, Tuple

# Forkserver over fork: children must not inherit the parent's event loop,
# sockets, or DB pool fds. Over spawn: repeated launches skip re-running the
# interpreter setup. The forkserver process itself starts lazily on first
# Process(); creating the context at import time costs nothing. Each child
# imports the target's module fresh -- keep targets in modules that import
# cheaply (parsing.pdf, the chunker stack), not in modules dragging in HTTP
# clients and LLM SDKs.
_forkserver_ctx = multiprocessing.get_context("forkserver")

# Default address-space cap, inherited from the original extraction guard.
# Rationale for 1.5GB on the 3.8GB RAM + 6GB swap box: children die cleanly
# with MemoryError instead of OOM-killing the parent, and only monster
# 1000+ page OCR jobs ever approach it. Call sites doing lighter work
# (text-layer chunking) should pass a tighter cap.
DEFAULT_RLIMIT_BYTES = int(1.5 * 1024 * 1024 * 1024)

# Give the parent this long to observe a child's exit after its result (or
# kill signal) lands; anything longer means the child is unkillable-wedged.
_JOIN_TIMEOUT_SECONDS = 30


class GuardError(Exception):
    """Base for guard failures. Catch this to handle any guarded outcome."""


class GuardTimeout(GuardError):
    """The child produced no result within the deadline and was killed."""


class GuardCrashed(GuardError):
    """The child died without reporting (segfault, OOM kill, os._exit)."""

    def __init__(self, message: str, exitcode: Optional[int] = None):
        super().__init__(message)
        self.exitcode = exitcode


class GuardTaskError(GuardError):
    """The target raised inside the child; message and type survive the hop."""

    def __init__(self, message: str, error_type: Optional[str] = None):
        super().__init__(message)
        self.error_type = error_type


def _guard_worker(
    result_queue,
    rlimit_bytes: int,
    target: Callable[..., Any],
    args: Tuple[Any, ...],
    kwargs: Dict[str, Any],
) -> None:
    """Child-process entrypoint: cap resources, run target, report once."""
    resource.setrlimit(resource.RLIMIT_AS, (rlimit_bytes, rlimit_bytes))

    # Mark this child as a preferred OOM victim. The conductor parent sets
    # itself to -500; we override to +500 so under system-wide memory
    # pressure the kernel kills the actual memory hog instead of orphaning
    # the coordinator. Raising your own oom_score_adj toward more-killable
    # never requires capabilities.
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("500")
    except OSError:
        pass  # Non-Linux or restricted /proc -- worker still functions

    try:
        result_queue.put(("ok", target(*args, **kwargs)))
    except Exception as e:
        result_queue.put(("error", str(e) or type(e).__name__, type(e).__name__))


def run_guarded(
    target: Callable[..., Any],
    args: Tuple[Any, ...] = (),
    kwargs: Optional[Dict[str, Any]] = None,
    *,
    timeout: float = 600.0,
    rlimit_bytes: int = DEFAULT_RLIMIT_BYTES,
) -> Any:
    """Run target(*args, **kwargs) in a resource-capped subprocess.

    target must be a module-level callable (pickled by reference; the child
    imports its module) and args/kwargs/return value must pickle. Blocks the
    calling thread for up to `timeout` seconds.

    Raises GuardTimeout (child killed after the deadline), GuardCrashed
    (child died silently -- segfault, OOM), or GuardTaskError (target raised;
    original message and type attached). Anything else propagates as-is.
    """
    result_queue = _forkserver_ctx.Queue()
    proc = _forkserver_ctx.Process(
        target=_guard_worker,
        args=(result_queue, rlimit_bytes, target, args, kwargs or {}),
    )
    try:
        proc.start()

        # Drain the queue BEFORE join: the queue rides a pipe (64KB buffer on
        # Linux), so a result bigger than the buffer blocks the child's put()
        # until the parent reads. A parent blocked on join() at that moment
        # deadlocks both sides until the timeout.
        #
        # Poll in short slices rather than one long get: a child that dies
        # WITHOUT reporting (segfault, OOM kill) never puts anything, and a
        # single blocking get would sit out the full timeout before anyone
        # noticed -- the predecessor of this module did exactly that, turning
        # every segfault into a silent 600s stall.
        deadline = time.monotonic() + timeout
        target_name = getattr(target, "__name__", repr(target))
        result_msg = None
        while result_msg is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if proc.is_alive():
                    proc.kill()
                proc.join(timeout=10)
                raise GuardTimeout(
                    f"{target_name} subprocess timed out after {timeout:.0f}s"
                )
            try:
                result_msg = result_queue.get(timeout=min(1.0, remaining))
            except Empty:
                if not proc.is_alive():
                    # Dead child. Its result may still be in flight through
                    # the feeder thread/pipe -- one final grace read before
                    # declaring a silent crash.
                    try:
                        result_msg = result_queue.get(timeout=1.0)
                    except Empty:
                        proc.join(timeout=10)
                        raise GuardCrashed(
                            f"{target_name} subprocess crashed "
                            f"(exit code {proc.exitcode})",
                            exitcode=proc.exitcode,
                        )

        proc.join(timeout=_JOIN_TIMEOUT_SECONDS)
        if proc.is_alive():
            proc.kill()
            proc.join()

        if proc.exitcode != 0 and proc.exitcode is not None:
            raise GuardCrashed(
                f"{target_name} subprocess crashed (exit code {proc.exitcode})",
                exitcode=proc.exitcode,
            )

        status, *data = result_msg
        if status == "error":
            raise GuardTaskError(data[0], error_type=data[1])
        return data[0]
    finally:
        # Release the Queue's pipe fds, semaphores, and feeder thread now.
        # Without explicit close each run leaks ~4 fds until non-deterministic
        # GC; over thousands of runs in a long process-cities session that
        # was a significant contributor to the 2026-04-10 parent RSS bloat.
        try:
            result_queue.close()
            result_queue.join_thread()
        except Exception:
            pass
