"""Typed outcomes shared by every queue execution surface.

Queue state must be driven by the semantic result of the work, not by the
incidental fact that a coroutine returned without raising.  The processor still
has legacy functions that return counters; ``JobOutcome.from_stats`` is the
compatibility boundary while those functions are moved to explicit outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class OutcomeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"


@dataclass(frozen=True, slots=True)
class JobOutcome:
    status: OutcomeStatus
    stats: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    error_type: str | None = None

    @property
    def is_success(self) -> bool:
        return self.status is OutcomeStatus.SUCCEEDED

    @property
    def should_retry(self) -> bool:
        return self.status in {
            OutcomeStatus.PARTIAL,
            OutcomeStatus.RETRYABLE_FAILURE,
        }

    @classmethod
    def succeeded(cls, stats: Mapping[str, Any] | None = None) -> "JobOutcome":
        return cls(OutcomeStatus.SUCCEEDED, _integer_stats(stats))

    @classmethod
    def retryable_failure(
        cls,
        error: Exception | str,
        stats: Mapping[str, Any] | None = None,
    ) -> "JobOutcome":
        return cls(
            OutcomeStatus.RETRYABLE_FAILURE,
            _integer_stats(stats),
            error=str(error),
            error_type=type(error).__name__ if isinstance(error, Exception) else None,
        )

    @classmethod
    def terminal_failure(
        cls,
        error: Exception | str,
        stats: Mapping[str, Any] | None = None,
    ) -> "JobOutcome":
        return cls(
            OutcomeStatus.TERMINAL_FAILURE,
            _integer_stats(stats),
            error=str(error),
            error_type=type(error).__name__ if isinstance(error, Exception) else None,
        )

    @classmethod
    def from_stats(cls, stats: Mapping[str, Any] | None) -> "JobOutcome":
        """Translate legacy processing counters into queue semantics.

        Any failed unit keeps the work recoverable.  A mixed result is partial;
        a result with no completed/skipped units is a retryable failure.  Skips
        count as handled work because filters and already-current summaries are
        valid terminal dispositions for those units.
        """
        normalized = _integer_stats(stats)
        failed = normalized.get("items_failed", 0)
        if failed <= 0:
            return cls.succeeded(normalized)

        handled = normalized.get("items_new", 0) + normalized.get("items_skipped", 0)
        processed = normalized.get("items_processed", 0)
        if handled > 0 or processed > failed:
            return cls(
                OutcomeStatus.PARTIAL,
                normalized,
                error=f"{failed} processing unit(s) failed",
            )
        return cls.retryable_failure(
            f"{failed} processing unit(s) failed", normalized
        )


def _integer_stats(stats: Mapping[str, Any] | None) -> dict[str, int]:
    if not stats:
        return {}
    normalized: dict[str, int] = {}
    for key, value in stats.items():
        if isinstance(value, bool):
            normalized[key] = int(value)
        elif isinstance(value, int):
            normalized[key] = value
    return normalized
