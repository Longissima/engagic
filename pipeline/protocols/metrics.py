"""Metrics Protocol - Interface for metrics collection without concrete dependency

This protocol enables dependency injection of metrics throughout the pipeline,
allowing components to be tested and run without the server module.
"""

from typing import Protocol, Any, ContextManager
from contextlib import contextmanager


class LabeledCounter(Protocol):
    def labels(self, **kwargs: Any) -> "LabeledCounter": ...
    def inc(self, amount: float = 1) -> None: ...


class LabeledHistogram(Protocol):
    def labels(self, **kwargs: Any) -> "LabeledHistogram": ...
    def observe(self, value: float) -> None: ...
    def time(self) -> ContextManager: ...


class MetricsCollector(Protocol):
    """Unified metrics interface for all pipeline components

    Used by:
    - pipeline/processor.py - Queue and processing metrics
    - pipeline/fetcher.py - Vendor sync metrics
    - analysis/llm/summarizer.py - LLM API metrics
    - vendors/adapters/base_adapter_async.py - Vendor request metrics
    """
    # Queue metrics
    queue_jobs_processed: LabeledCounter
    processing_duration: LabeledHistogram

    # Vendor metrics
    vendor_requests: LabeledCounter
    vendor_request_duration: LabeledHistogram
    meetings_synced: LabeledCounter
    items_extracted: LabeledCounter
    matters_tracked: LabeledCounter

    # Shared document acquisition metrics
    document_acquisitions: LabeledCounter
    document_acquisition_duration: LabeledHistogram

    def record_error(self, component: str, error: Exception) -> None: ...

    def record_llm_call(
        self,
        model: str,
        prompt_type: str,
        duration_seconds: float,
        input_tokens: int,
        output_tokens: int,
        cost_dollars: float,
        success: bool = True
    ) -> None: ...


class _NullCounter:
    def labels(self, **kwargs: Any) -> "_NullCounter":
        return self

    def inc(self, amount: float = 1) -> None:
        pass


class _NullHistogram:
    def labels(self, **kwargs: Any) -> "_NullHistogram":
        return self

    def observe(self, value: float) -> None:
        pass

    @contextmanager
    def time(self):
        yield


class NullMetrics:
    """No-op metrics for testing or standalone use"""

    def __init__(self):
        self.queue_jobs_processed = _NullCounter()
        self.processing_duration = _NullHistogram()
        self.vendor_requests = _NullCounter()
        self.vendor_request_duration = _NullHistogram()
        self.meetings_synced = _NullCounter()
        self.items_extracted = _NullCounter()
        self.matters_tracked = _NullCounter()
        self.document_acquisitions = _NullCounter()
        self.document_acquisition_duration = _NullHistogram()

    def record_error(self, component: str, error: Exception) -> None:
        pass

    def record_llm_call(
        self,
        model: str,
        prompt_type: str,
        duration_seconds: float,
        input_tokens: int,
        output_tokens: int,
        cost_dollars: float,
        success: bool = True
    ) -> None:
        pass
