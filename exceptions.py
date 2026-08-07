"""
Custom Exception Hierarchy - Domain-specific error types

Provides clear, typed exceptions for different failure modes across the system.
All custom exceptions inherit from EngagicError for easy catching.

Design Philosophy:
- Exceptions are data: Include context for debugging
- Fail explicitly: Better to raise specific exception than generic
- Catch specifically: Handler can distinguish error types
- Log contextually: Exception attributes enable rich logging
"""

from typing import Optional, Dict, Any


class EngagicError(Exception):
    """Base exception for all Engagic errors

    All custom exceptions inherit from this, enabling:
    - Catch all Engagic errors with single except clause
    - Distinguish our errors from library errors
    - Add common attributes (context, original_error)
    - Check if error is retryable via is_retryable property
    """

    # Default: errors are not retryable (permanent failure)
    # Subclasses can override this for transient failures
    _retryable: bool = False

    def __init__(self, message: str, context: Optional[Dict[str, Any]] = None):
        self.context = context or {}
        super().__init__(message)

    @property
    def is_retryable(self) -> bool:
        """Check if this error represents a transient failure that should be retried.

        Returns:
            True for transient failures (network, rate limits, timeouts)
            False for permanent failures (parse errors, validation, missing data)
        """
        return self._retryable

    def __str__(self):
        base_msg = super().__str__()
        if self.context:
            context_str = ", ".join(f"{k}={v}" for k, v in self.context.items())
            return f"{base_msg} (context: {context_str})"
        return base_msg


# ========== Database Errors ==========


class DatabaseError(EngagicError):
    """Database operation failures

    Examples:
    - Connection failures
    - Query errors
    - Transaction rollbacks
    - Data integrity violations
    """
    pass


class DatabaseConnectionError(DatabaseError):
    """Failed to establish or maintain database connection"""
    _retryable = True  # Connection issues are often transient


class DataIntegrityError(DatabaseError):
    """Data integrity constraint violation

    Examples:
    - Foreign key violations
    - Unique constraint violations
    - Check constraint failures
    """

    def __init__(self, message: str, table: Optional[str] = None, constraint: Optional[str] = None):
        self.table = table
        self.constraint = constraint
        context = {}
        if table:
            context['table'] = table
        if constraint:
            context['constraint'] = constraint
        super().__init__(message, context)


class DuplicateEntityError(DataIntegrityError):
    """Attempted to create entity that already exists

    Examples:
    - Duplicate city banana
    - Duplicate meeting ID
    - Duplicate matter ID
    """

    def __init__(self, message: str, entity_type: str, entity_id: str, table: Optional[str] = None):
        self.entity_type = entity_type
        self.entity_id = entity_id
        super().__init__(message, table=table, constraint="unique")
        self.context['entity_type'] = entity_type
        self.context['entity_id'] = entity_id


class InvalidForeignKeyError(DataIntegrityError):
    """Referenced entity does not exist

    Examples:
    - Meeting ID not found when storing agenda items
    - City banana not found when storing meeting
    """

    def __init__(self, message: str, referenced_table: str, referenced_id: str, table: Optional[str] = None):
        self.referenced_table = referenced_table
        self.referenced_id = referenced_id
        super().__init__(message, table=table, constraint="foreign_key")
        self.context['referenced_table'] = referenced_table
        self.context['referenced_id'] = referenced_id


class StaleJobError(DatabaseError):
    """Queue job is stale or has been claimed by another worker

    Examples:
    - Job already processed
    - Job claimed by another worker
    - Job status changed during processing
    """
    _retryable = True  # Worker should get fresh job from queue

    def __init__(self, message: str, queue_id: int, expected_status: Optional[str] = None, actual_status: Optional[str] = None):
        self.queue_id = queue_id
        self.expected_status = expected_status
        self.actual_status = actual_status
        context = {'queue_id': queue_id}
        if expected_status:
            context['expected_status'] = expected_status
        if actual_status:
            context['actual_status'] = actual_status
        super().__init__(message, context)


# ========== Vendor Errors ==========


class VendorError(EngagicError):
    """Vendor adapter failures

    Includes context about which vendor and city failed.
    """

    def __init__(
        self,
        message: str,
        vendor: str,
        city_slug: Optional[str] = None,
        original_error: Optional[Exception] = None
    ):
        self.vendor = vendor
        self.city_slug = city_slug
        self.original_error = original_error

        context = {'vendor': vendor}
        if city_slug:
            context['city_slug'] = city_slug
        if original_error:
            context['original_error'] = str(original_error)

        super().__init__(message, context)


class VendorHTTPError(VendorError):
    """HTTP request to vendor failed

    Examples:
    - 404 Not Found
    - 403 Forbidden
    - 500 Server Error
    - Timeout

    Retryable for:
    - 5xx errors (server issues)
    - Timeouts (network transient)
    - Connection errors

    Not retryable for:
    - 4xx errors (client issues, bad request, not found)
    """

    def __init__(
        self,
        message: str,
        vendor: str,
        status_code: Optional[int] = None,
        url: Optional[str] = None,
        city_slug: Optional[str] = None,
    ):
        self.status_code = status_code
        self.url = url

        super().__init__(message, vendor, city_slug)

        if status_code:
            self.context['status_code'] = status_code
        if url:
            self.context['url'] = url

    @property
    def is_retryable(self) -> bool:
        """5xx errors and timeouts are retryable, 4xx are not"""
        if self.status_code is None:
            # No status code = network/timeout error, retryable
            return True
        return self.status_code >= 500


class VendorParsingError(VendorError):
    """Failed to parse vendor HTML/JSON

    Examples:
    - Expected element not found
    - Malformed HTML structure
    - JSON decode error
    """
    pass


# ========== Processing Errors ==========


class ProcessingError(EngagicError):
    """Processing pipeline failures

    Covers extraction, summarization, and queue processing errors.
    """
    pass


class ExtractionError(ProcessingError):
    """Failed to extract text from document

    Examples:
    - PDF extraction failure
    - Corrupted file
    - Unsupported format
    """

    def __init__(
        self,
        message: str,
        document_url: Optional[str] = None,
        document_type: Optional[str] = None,
        original_error: Optional[Exception] = None
    ):
        self.document_url = document_url
        self.document_type = document_type
        self.original_error = original_error

        context = {}
        if document_url:
            context['document_url'] = document_url
        if document_type:
            context['document_type'] = document_type
        if original_error:
            context['original_error'] = str(original_error)

        super().__init__(message, context)


class DocumentDownloadError(ExtractionError):
    """Document bytes could not be acquired or resolved to a supported file."""

    _retryable = True

    def __init__(
        self,
        message: str,
        *,
        retryable: Optional[bool] = None,
        status_code: Optional[int] = None,
        **kwargs,
    ):
        # Network failures have no status and are transient by default. For
        # HTTP responses, retry only statuses whose semantics are transient;
        # deterministic 4xx failures use the caller's bounded retry policy.
        if retryable is None:
            retryable = (
                status_code is None
                or status_code in (408, 425, 429)
                or 500 <= status_code <= 599
            )
        self._retryable = retryable
        self.status_code = status_code
        super().__init__(message, **kwargs)
        if status_code is not None:
            self.context["status_code"] = status_code


class LLMError(ProcessingError):
    """LLM API failures

    Examples:
    - API rate limit
    - API timeout
    - Invalid response format
    - Model quota exceeded
    """

    def __init__(
        self,
        message: str,
        model: Optional[str] = None,
        prompt_type: Optional[str] = None,
        original_error: Optional[Exception] = None
    ):
        self.model = model
        self.prompt_type = prompt_type
        self.original_error = original_error

        context = {}
        if model:
            context['model'] = model
        if prompt_type:
            context['prompt_type'] = prompt_type
        if original_error:
            context['original_error'] = str(original_error)

        super().__init__(message, context)


class QueueError(ProcessingError):
    """Queue processing failures

    Examples:
    - Failed to enqueue job
    - Job deserialization error
    - Dead letter queue full
    """

    def __init__(
        self,
        message: str,
        queue_id: Optional[int] = None,
        job_type: Optional[str] = None
    ):
        self.queue_id = queue_id
        self.job_type = job_type

        context = {}
        if queue_id:
            context['queue_id'] = queue_id
        if job_type:
            context['job_type'] = job_type

        super().__init__(message, context)


# ========== Parsing Errors ==========


class ParsingError(EngagicError):
    """HTML/PDF/JSON parsing failures

    Generic parsing error for various document types.
    """

    def __init__(
        self,
        message: str,
        parser_type: Optional[str] = None,
        source: Optional[str] = None
    ):
        self.parser_type = parser_type
        self.source = source

        context = {}
        if parser_type:
            context['parser_type'] = parser_type
        if source:
            context['source'] = source

        super().__init__(message, context)


# ========== Configuration Errors ==========


class ConfigurationError(EngagicError):
    """Configuration or environment errors

    Examples:
    - Missing required env var
    - Invalid configuration value
    - Missing API key
    """

    def __init__(self, message: str, config_key: Optional[str] = None):
        self.config_key = config_key
        context = {}
        if config_key:
            context['config_key'] = config_key
        super().__init__(message, context)


# ========== Validation Errors ==========


class ValidationError(EngagicError):
    """Data validation failures

    Examples:
    - Invalid input format
    - Missing required field
    - Value out of range
    """

    def __init__(self, message: str, field: Optional[str] = None, value: Optional[Any] = None):
        self.field = field
        self.value = value

        context = {}
        if field:
            context['field'] = field
        if value is not None:
            context['value'] = str(value)

        super().__init__(message, context)


# ========== Rate Limiting Errors ==========


class RateLimitError(EngagicError):
    """Rate limit exceeded

    Examples:
    - Vendor rate limit
    - API rate limit
    - Internal rate limit

    Always retryable (wait and try again).
    """

    _retryable = True  # Rate limits are always transient

    def __init__(
        self,
        message: str,
        service: Optional[str] = None,
        retry_after: Optional[int] = None
    ):
        self.service = service
        self.retry_after = retry_after

        context = {}
        if service:
            context['service'] = service
        if retry_after:
            context['retry_after'] = retry_after

        super().__init__(message, context)


# Confidence level: 8/10
# This exception hierarchy covers the main error categories in the system.
# It's extensible (easy to add new exception types) and provides rich context
# for debugging. The main uncertainty is whether we need more granular
# exceptions for specific vendor types or processing stages.
