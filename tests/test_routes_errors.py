"""Unit tests for unflincher.routes.errors: the shared mapping from stable generation-safety
exceptions to HTTP responses used by every route that calls into the preflight/maintenance/lease
primitives."""
import pytest

from unflincher.context_budget import ContextTooLargeError, ModelLimitsUnavailableError
from unflincher.db import (
    ArchiveChangedError,
    ItemJobMismatchError,
    MaintenanceLockedError,
    RequestFormatChangedError,
    RequestLeaseExpiredError,
    StaleOrSupersededRetryError,
    TargetBusyError,
)
from unflincher.routes.errors import generation_safety_http_exception


def test_context_too_large_maps_to_413_with_estimate_and_limit():
    exc = ContextTooLargeError(model="m", estimated_tokens=500, limit=100, target_kind="entry_commentary", target_id="1")
    http_exc = generation_safety_http_exception(exc)
    assert http_exc.status_code == 413
    assert http_exc.detail["reason"] == "context_too_large"
    assert http_exc.detail["estimated_tokens"] == 500
    assert http_exc.detail["limit"] == 100
    assert http_exc.detail["model"] == "m"


def test_model_limits_unavailable_maps_to_503():
    exc = ModelLimitsUnavailableError("m", "boom")
    http_exc = generation_safety_http_exception(exc)
    assert http_exc.status_code == 503
    assert http_exc.detail["reason"] == "model_limits_unavailable"


def test_maintenance_locked_maps_to_503_retryable():
    http_exc = generation_safety_http_exception(MaintenanceLockedError("locked"))
    assert http_exc.status_code == 503
    assert http_exc.detail["reason"] == "maintenance_locked"


def test_target_busy_maps_to_409_with_target_key():
    http_exc = generation_safety_http_exception(TargetBusyError("entry:1"))
    assert http_exc.status_code == 409
    assert http_exc.detail["reason"] == "target_busy"
    assert http_exc.detail["target_key"] == "entry:1"


def test_archive_changed_maps_to_409():
    http_exc = generation_safety_http_exception(ArchiveChangedError("changed"))
    assert http_exc.status_code == 409
    assert http_exc.detail["reason"] == "archive_changed"


def test_request_format_changed_maps_to_409():
    http_exc = generation_safety_http_exception(RequestFormatChangedError("changed"))
    assert http_exc.status_code == 409
    assert http_exc.detail["reason"] == "request_format_changed"


def test_stale_or_superseded_retry_maps_to_409():
    http_exc = generation_safety_http_exception(StaleOrSupersededRetryError("stale"))
    assert http_exc.status_code == 409
    assert http_exc.detail["reason"] == "stale_or_superseded"


def test_request_lease_expired_maps_to_409():
    http_exc = generation_safety_http_exception(RequestLeaseExpiredError("expired"))
    assert http_exc.status_code == 409
    assert http_exc.detail["reason"] == "request_lease_expired"


def test_item_job_mismatch_maps_to_404():
    http_exc = generation_safety_http_exception(ItemJobMismatchError(5, 1, 2))
    assert http_exc.status_code == 404
    assert http_exc.detail["reason"] == "item_job_mismatch"


def test_unmapped_exception_raises_type_error():
    with pytest.raises(TypeError):
        generation_safety_http_exception(RuntimeError("not one of ours"))
