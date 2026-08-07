from pipeline.outcomes import JobOutcome, OutcomeStatus


def test_success_when_no_processing_units_failed():
    outcome = JobOutcome.from_stats(
        {"items_processed": 4, "items_new": 2, "items_skipped": 2, "items_failed": 0}
    )

    assert outcome.status is OutcomeStatus.SUCCEEDED
    assert outcome.is_success
    assert not outcome.should_retry


def test_total_business_failure_is_retryable_even_without_exception():
    outcome = JobOutcome.from_stats(
        {"items_processed": 3, "items_new": 0, "items_skipped": 0, "items_failed": 3}
    )

    assert outcome.status is OutcomeStatus.RETRYABLE_FAILURE
    assert outcome.should_retry
    assert not outcome.is_success


def test_mixed_result_is_partial_and_recoverable():
    outcome = JobOutcome.from_stats(
        {"items_processed": 4, "items_new": 2, "items_skipped": 1, "items_failed": 1}
    )

    assert outcome.status is OutcomeStatus.PARTIAL
    assert outcome.should_retry


def test_terminal_failure_is_not_retried():
    outcome = JobOutcome.terminal_failure(ValueError("invalid identity"))

    assert outcome.status is OutcomeStatus.TERMINAL_FAILURE
    assert not outcome.should_retry
    assert outcome.error_type == "ValueError"


def test_abandoned_claim_is_visible_but_not_retried_or_successful():
    outcome = JobOutcome.abandoned("claim superseded", {"items_new": 1})

    assert outcome.status is OutcomeStatus.ABANDONED
    assert not outcome.is_success
    assert not outcome.should_retry
    assert outcome.error_type == "ClaimLost"


def test_bounded_scalar_diagnostics_survive_without_affecting_classification():
    outcome = JobOutcome.from_stats(
        {
            "items_new": 1,
            "items_failed": 0,
            "model": "flash",
            "cached": True,
            "provider_wait_ms": 12.5,
            "nested": {"ignored": True},
        }
    )

    assert outcome.is_success
    assert outcome.stats == {
        "items_new": 1,
        "items_failed": 0,
        "model": "flash",
        "cached": True,
        "provider_wait_ms": 12.5,
    }
