"""Typed outcomes shared by every queue execution surface.

Queue state must be driven by the semantic result of the work, not by the
incidental fact that a coroutine returned without raising.  The processor still
has legacy functions that return counters; ``JobOutcome.from_stats`` is the
compatibility boundary while those functions are moved to explicit outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import math
from typing import Any, Mapping


class OutcomeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"
    ABANDONED = "abandoned"


@dataclass(frozen=True, slots=True)
class JobOutcome:
    status: OutcomeStatus
    stats: dict[str, int | float | bool | str] = field(default_factory=dict)
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
        return cls(OutcomeStatus.SUCCEEDED, _safe_metrics(stats))

    @classmethod
    def retryable_failure(
        cls,
        error: Exception | str,
        stats: Mapping[str, Any] | None = None,
    ) -> "JobOutcome":
        return cls(
            OutcomeStatus.RETRYABLE_FAILURE,
            _safe_metrics(stats),
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
            _safe_metrics(stats),
            error=str(error),
            error_type=type(error).__name__ if isinstance(error, Exception) else None,
        )

    @classmethod
    def abandoned(
        cls,
        error: Exception | str,
        stats: Mapping[str, Any] | None = None,
    ) -> "JobOutcome":
        """The attempt lost ownership; current desired work remains elsewhere."""
        return cls(
            OutcomeStatus.ABANDONED,
            _safe_metrics(stats),
            error=str(error),
            error_type=(
                type(error).__name__ if isinstance(error, Exception) else "ClaimLost"
            ),
        )

    @classmethod
    def from_stats(cls, stats: Mapping[str, Any] | None) -> "JobOutcome":
        """Translate legacy processing counters into queue semantics.

        Any failed unit keeps the work recoverable.  A mixed result is partial;
        a result with no completed/skipped units is a retryable failure.  Skips
        count as handled work because filters and already-current summaries are
        valid terminal dispositions for those units.
        """
        normalized = _safe_metrics(stats)
        failed = _counter(normalized, "items_failed")
        if failed <= 0:
            return cls.succeeded(normalized)

        handled = _counter(normalized, "items_new") + _counter(
            normalized, "items_skipped"
        )
        processed = _counter(normalized, "items_processed")
        if handled > 0 or processed > failed:
            return cls(
                OutcomeStatus.PARTIAL,
                normalized,
                error=f"{failed} processing unit(s) failed",
            )
        return cls.retryable_failure(
            f"{failed} processing unit(s) failed", normalized
        )


def _counter(metrics: Mapping[str, Any], key: str) -> int:
    value = metrics.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _safe_metrics(
    stats: Mapping[str, Any] | None,
) -> dict[str, int | float | bool | str]:
    """Keep bounded scalar telemetry suitable for durable JSON metrics."""
    if not stats:
        return {}
    normalized: dict[str, int | float | bool | str] = {}
    for key, value in stats.items():
        metric_key = str(key)[:128]
        if isinstance(value, bool):
            normalized[metric_key] = value
        elif isinstance(value, int):
            normalized[metric_key] = value
        elif isinstance(value, float) and math.isfinite(value):
            normalized[metric_key] = value
        elif isinstance(value, str):
            normalized[metric_key] = value[:512]
    return normalized
