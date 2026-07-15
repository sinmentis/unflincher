"""Shared mapping from this app's stable generation-safety exceptions (context budget,
maintenance, leases, archive snapshot) to HTTP responses -- the ONE place every route that calls
into llm.py's prepare_*_request / regen_enqueue.py / db.py's lease and retry primitives converts
those exceptions, so the status code and detail shape for e.g. "context too large" can never
drift between Entry Reflection, Life Report, Prompt Workshop preview, and Conversation routes.

Every mapped response is stable and no-write: by the time any of these exceptions reaches a
route, the underlying call has already guaranteed nothing was durably written (see each
exception's own docstring in context_budget.py / db.py)."""
from fastapi import HTTPException

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


def generation_safety_http_exception(exc: Exception) -> HTTPException:
    """Return the HTTPException a route should raise for one of this app's stable
    generation-safety errors. Callers should raise the RETURNED exception (not exc itself), e.g.:

        try:
            prepared = await llm.prepare_commentary_request(...)
        except (ContextTooLargeError, ModelLimitsUnavailableError) as exc:
            raise generation_safety_http_exception(exc) from exc
    """
    if isinstance(exc, ContextTooLargeError):
        return HTTPException(status_code=413, detail={
            "reason": "context_too_large",
            "estimated_tokens": exc.estimated_tokens,
            "limit": exc.limit,
            "model": exc.model,
            "target_kind": exc.target_kind,
            "target_id": exc.target_id,
        })
    if isinstance(exc, ModelLimitsUnavailableError):
        return HTTPException(status_code=503, detail={
            "reason": "model_limits_unavailable", "model": exc.model,
        })
    if isinstance(exc, MaintenanceLockedError):
        return HTTPException(status_code=503, detail={"reason": "maintenance_locked"})
    if isinstance(exc, TargetBusyError):
        return HTTPException(status_code=409, detail={
            "reason": "target_busy", "target_key": exc.target_key,
        })
    if isinstance(exc, ArchiveChangedError):
        return HTTPException(status_code=409, detail={"reason": "archive_changed"})
    if isinstance(exc, RequestFormatChangedError):
        return HTTPException(status_code=409, detail={"reason": "request_format_changed"})
    if isinstance(exc, StaleOrSupersededRetryError):
        return HTTPException(status_code=409, detail={"reason": "stale_or_superseded"})
    if isinstance(exc, RequestLeaseExpiredError):
        return HTTPException(status_code=409, detail={"reason": "request_lease_expired"})
    if isinstance(exc, ItemJobMismatchError):
        return HTTPException(status_code=404, detail={"reason": "item_job_mismatch"})
    raise TypeError(f"no HTTP mapping registered for {type(exc).__name__}")
