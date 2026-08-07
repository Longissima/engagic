"""
Prometheus Metrics Module

Provides instrumentation for all core operations:
- Meeting sync and item extraction
- Matter tracking
- LLM API calls and costs
- Queue health
- Error tracking

Usage:
    from server.metrics import metrics
    metrics.meetings_synced.labels(city="sfCA", vendor="legistar").inc()
    with metrics.processing_duration.labels(job_type="meeting").time():
        process_meeting()

Confidence: 9/10 - Standard Prometheus instrumentation patterns
"""

from prometheus_client import Counter, Histogram, Gauge, generate_latest, REGISTRY


class EngagicMetrics:
    """Centralized metrics for Engagic pipeline and API"""

    def __init__(self):
        # Sync metrics
        self.meetings_synced = Counter(
            'engagic_meetings_synced_total',
            'Total meetings synced from vendors',
            ['city', 'vendor']
        )

        self.items_extracted = Counter(
            'engagic_items_extracted_total',
            'Total agenda items extracted',
            ['city', 'vendor']
        )

        self.matters_tracked = Counter(
            'engagic_matters_tracked_total',
            'Total matters tracked across meetings',
            ['city']
        )

        # Processing metrics
        self.processing_duration = Histogram(
            'engagic_processing_duration_seconds',
            'Processing duration by job type',
            ['job_type'],
            buckets=[1, 5, 10, 30, 60, 120, 300, 600]
        )

        self.pdf_extraction_duration = Histogram(
            'engagic_pdf_extraction_seconds',
            'PDF text extraction duration',
            ['document_type'],
            buckets=[0.5, 1, 2, 5, 10, 30, 60]
        )

        self.document_acquisitions = Counter(
            'engagic_document_acquisitions_total',
            'Document acquisitions by pipeline component, outcome, and observed format',
            ['component', 'outcome', 'document_type'],
        )

        self.document_acquisition_duration = Histogram(
            'engagic_document_acquisition_seconds',
            'Document acquisition duration by pipeline component and outcome',
            ['component', 'outcome', 'document_type'],
            buckets=[0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 300],
        )

        # LLM metrics
        self.llm_api_calls = Counter(
            'engagic_llm_api_calls_total',
            'Total LLM API calls',
            ['model', 'prompt_type', 'status']
        )

        self.llm_api_duration = Histogram(
            'engagic_llm_api_duration_seconds',
            'LLM API call duration',
            ['model', 'prompt_type'],
            buckets=[1, 2, 5, 10, 20, 30, 60]
        )

        self.llm_api_tokens = Counter(
            'engagic_llm_api_tokens_total',
            'Total tokens consumed',
            ['model', 'token_type']  # token_type: input/output
        )

        self.llm_api_cost = Counter(
            'engagic_llm_api_cost_dollars',
            'Total LLM API cost in dollars',
            ['model']
        )

        # Queue metrics
        self.queue_size = Gauge(
            'engagic_queue_size',
            'Current queue size by status',
            ['status']
        )

        self.queue_jobs_processed = Counter(
            'engagic_queue_jobs_processed_total',
            'Total queue jobs processed',
            ['job_type', 'status']  # status: completed/failed/dead_letter
        )

        # API metrics
        self.api_requests = Counter(
            'engagic_api_requests_total',
            'Total API requests',
            ['endpoint', 'method', 'status_code']
        )

        self.api_request_duration = Histogram(
            'engagic_api_request_duration_seconds',
            'API request duration',
            ['endpoint', 'method'],
            buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0]
        )

        # User behavior metrics
        self.page_views = Counter(
            'engagic_page_views_total',
            'Total page views by type',
            ['page_type']  # search, city, matter, meeting, state
        )

        self.search_queries = Counter(
            'engagic_search_queries_total',
            'Total search queries by type',
            ['query_type']  # zipcode, city_name, state
        )

        self.matter_engagement = Counter(
            'engagic_matter_engagement_total',
            'Matter page engagement actions',
            ['action']  # view, timeline
        )

        # Error metrics
        self.errors = Counter(
            'engagic_errors_total',
            'Total errors by component and type',
            ['component', 'error_type']
        )

        # Vendor metrics
        self.vendor_requests = Counter(
            'engagic_vendor_requests_total',
            'Total vendor API/scraping requests',
            ['vendor', 'status']
        )

        self.vendor_request_duration = Histogram(
            'engagic_vendor_request_duration_seconds',
            'Vendor request duration',
            ['vendor'],
            buckets=[0.5, 1, 2, 5, 10, 30, 60]
        )

        # Database metrics
        self.db_operations = Counter(
            'engagic_db_operations_total',
            'Database operations',
            ['operation', 'table']
        )

        self.db_operation_duration = Histogram(
            'engagic_db_operation_duration_seconds',
            'Database operation duration',
            ['operation', 'table'],
            buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0]
        )

    def update_queue_sizes(self, queue_stats: dict):
        """Update queue size gauges from queue stats

        Args:
            queue_stats: Dict from UnifiedDatabase.get_queue_stats()
                        Keys: pending_count, processing_count, completed_count, etc.
        """
        for status in ['pending', 'processing', 'completed', 'failed', 'dead_letter']:
            count = queue_stats.get(f'{status}_count', 0)
            self.queue_size.labels(status=status).set(count)

    def record_llm_call(
        self,
        model: str,
        prompt_type: str,
        duration_seconds: float,
        input_tokens: int,
        output_tokens: int,
        cost_dollars: float,
        success: bool = True
    ):
        """Record a complete LLM API call with all metrics

        Args:
            model: Model name (e.g., "gemini-2.5-flash")
            prompt_type: Type of prompt (item/large/meeting)
            duration_seconds: API call duration
            input_tokens: Input tokens consumed
            output_tokens: Output tokens consumed
            cost_dollars: Total cost in dollars
            success: Whether the call succeeded
        """
        status = 'success' if success else 'error'

        self.llm_api_calls.labels(
            model=model,
            prompt_type=prompt_type,
            status=status
        ).inc()

        if success:
            self.llm_api_duration.labels(
                model=model,
                prompt_type=prompt_type
            ).observe(duration_seconds)

            self.llm_api_tokens.labels(model=model, token_type='input').inc(input_tokens)
            self.llm_api_tokens.labels(model=model, token_type='output').inc(output_tokens)

            self.llm_api_cost.labels(model=model).inc(cost_dollars)

    def record_error(self, component: str, error: Exception):
        """Record an error

        Args:
            component: Component name (vendor/processor/analyzer/database/api)
            error: Exception instance
        """
        error_type = type(error).__name__
        self.errors.labels(component=component, error_type=error_type).inc()


# Global metrics instance
metrics = EngagicMetrics()


def get_metrics_text() -> str:
    """Get Prometheus metrics in text format

    Returns:
        Metrics text suitable for /metrics endpoint
    """
    return generate_latest(REGISTRY).decode('utf-8')


def get_funnel_stats() -> dict:
    """Get user funnel stats as clean dict for dashboard display"""

    def get_counter_value(counter, labels: dict) -> int:
        try:
            return int(counter.labels(**labels)._value.get())
        except Exception:
            return 0

    def get_counter_total(counter) -> int:
        total = 0
        try:
            for sample in counter.collect()[0].samples:
                if sample.name.endswith('_total'):
                    total += int(sample.value)
        except Exception:
            pass
        return total

    return {
        "search": {
            "success": get_counter_value(metrics.search_queries, {"query_type": "success"}),
            "not_found": get_counter_value(metrics.search_queries, {"query_type": "not_found"}),
            "ambiguous": get_counter_value(metrics.search_queries, {"query_type": "ambiguous"}),
        },
        "pages": {
            "signup": get_counter_value(metrics.page_views, {"page_type": "signup"}),
            "deliberate": get_counter_value(metrics.page_views, {"page_type": "deliberate"}),
            "meeting": get_counter_value(metrics.page_views, {"page_type": "meeting_frontend"}),
        },
        "engagement": {
            "signup_complete": get_counter_value(metrics.matter_engagement, {"action": "signup"}),
            "item_expand": get_counter_value(metrics.matter_engagement, {"action": "item_expand"}),
            "flyer_click": get_counter_value(metrics.matter_engagement, {"action": "flyer_click"}),
            "matter_view": get_counter_value(metrics.matter_engagement, {"action": "matter_view"}),
        },
    }
