"""Guard contract tests: ok/task-error/timeout/crash/rlimit through real children.

Targets are module-level so the forkserver child can import them by
reference. These spawn real subprocesses -- each test costs ~100-300ms.
"""

import os
import time

import pytest

from parsing.subprocess_guard import (
    GuardCrashed,
    GuardTaskError,
    GuardTimeout,
    run_guarded,
)


def target_ok(a, b, scale=1):
    return {"sum": (a + b) * scale}


def target_big_result(n):
    # Larger than the 64KB pipe buffer: proves the drain-before-join order.
    return "x" * n


def target_raises():
    raise ValueError("boom from child")


def target_sleeps(seconds):
    time.sleep(seconds)
    return "never"


def target_exits():
    os._exit(7)


def target_allocates(n_bytes):
    block = bytearray(n_bytes)
    return len(block)


def test_ok_roundtrip_with_kwargs():
    assert run_guarded(target_ok, (2, 3), {"scale": 10}, timeout=30) == {"sum": 50}


def test_result_bigger_than_pipe_buffer():
    assert len(run_guarded(target_big_result, (1_000_000,), timeout=60)) == 1_000_000


def test_child_exception_surfaces_with_type():
    with pytest.raises(GuardTaskError) as exc:
        run_guarded(target_raises, timeout=30)
    assert "boom from child" in str(exc.value)
    assert exc.value.error_type == "ValueError"


def test_timeout_kills_child():
    start = time.monotonic()
    with pytest.raises(GuardTimeout):
        run_guarded(target_sleeps, (30,), timeout=2)
    assert time.monotonic() - start < 20  # killed, not waited out


def test_silent_death_is_crash():
    with pytest.raises(GuardCrashed) as exc:
        run_guarded(target_exits, timeout=30)
    assert exc.value.exitcode == 7


def test_rlimit_contains_allocation():
    # 512MB cap, 1GB allocation: the child dies with MemoryError inside the
    # worker (reported as GuardTaskError) or is killed outright (GuardCrashed).
    # Either way the parent survives and gets a typed failure.
    with pytest.raises((GuardTaskError, GuardCrashed)):
        run_guarded(
            target_allocates,
            (1024 * 1024 * 1024,),
            timeout=60,
            rlimit_bytes=512 * 1024 * 1024,
        )


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
