"""Orchestrates safe background-job creation for the single-entry and apply-all paths: prepares
and preflights every concrete request the job would generate (Entry Reflection per entry, plus
the Life Report for apply-all), then atomically enqueues it via db.enqueue_snapshot_regen_job.
Also owns the deep retry-ADMISSION interface (see retry_job_item_with_admission below), which
reconstructs and validates a failed item's request BEFORE ever touching the database's retry
state -- the acceptance requirement that retry REFUSE TO RUN on a format/fingerprint mismatch or
a capacity failure, rather than requeue-then-fail-later.

Owns the "archive changed between preflight and enqueue" rebuild-and-retry loop (see the plan's
Context budget and failure contract section) so callers (routes) do not each reimplement it: if
an entry is written after this module captured its preflight snapshot but before the atomic
enqueue transaction commits, db.enqueue_snapshot_regen_job raises db.ArchiveChangedError and
writes nothing; this module rebuilds the entire batch against the new archive and tries again, a
bounded number of times, before giving up and letting the error propagate as a stable 409.

This module intentionally contains NO system/user content assembly of its own -- see llm.py's
module docstring ("Keep request assembly concentrated/local"). It only calls llm.py's
build_*_envelope/prepare_*_request functions and hands the results to db.py."""
import logging

from unflincher import llm
from unflincher.context_budget import preflight_envelope
from unflincher.db import (
    ArchiveChangedError,
    ItemJobMismatchError,
    PreparedRegenTarget,
    RequestFormatChangedError,
    StaleOrSupersededRetryError,
    enqueue_snapshot_regen_job,
    get_entries_in_order,
    get_job_entry_snapshot,
    get_ordered_entry_ids,
    retry_failed_job_item,
)
from unflincher.request_envelope import fingerprint as envelope_fingerprint
from unflincher.request_envelope import matches_stored_assembly

logger = logging.getLogger(__name__)

# How many times to rebuild the whole batch against a freshly re-read archive after an
# ArchiveChangedError before giving up and letting it propagate. Bounded so a pathological
# write-every-time-we-retry scenario can never spin forever; in practice a single rebuild almost
# always succeeds since new entries arrive far less often than this loop can re-preflight.
_MAX_ARCHIVE_CHANGED_ATTEMPTS = 3


def _target_from_prepared(target_type: str, entry_id: int | None, prepared: llm.PreparedRequest) -> PreparedRegenTarget:
    return PreparedRegenTarget(
        target_type=target_type,
        entry_id=entry_id,
        request_format_version=prepared.envelope.assembly_version,
        request_fingerprint=envelope_fingerprint(prepared.envelope),
    )


async def enqueue_single_entry_job(
    conn, *, entry_id: int, prompt_version_id: int, persona_text: str, model: str, owner_token: str,
) -> int:
    """Prepare+preflight ONE entry_commentary target (with full-archive context, exactly like a
    real generation would see) and atomically enqueue a single-item snapshot-backed job for it.
    Raises context_budget.ContextTooLargeError / ModelLimitsUnavailableError (before any write),
    db.MaintenanceLockedError, db.TargetBusyError, or (after exhausting rebuild attempts)
    db.ArchiveChangedError."""
    last_error: ArchiveChangedError | None = None
    for attempt in range(_MAX_ARCHIVE_CHANGED_ATTEMPTS):
        preflight_entry_ids = get_ordered_entry_ids(conn)
        if entry_id not in preflight_entry_ids:
            raise ValueError(f"entry {entry_id} not found in the journal archive")
        all_entries = [dict(row) for row in get_entries_in_order(conn, preflight_entry_ids)]
        entry = next(e for e in all_entries if e["id"] == entry_id)

        prepared = await llm.prepare_commentary_request(entry, all_entries, persona_text, model)
        target = _target_from_prepared("entry_commentary", entry_id, prepared)

        try:
            job_id, _ = enqueue_snapshot_regen_job(
                conn, prompt_version_id=prompt_version_id, preflight_entry_ids=preflight_entry_ids,
                targets=[target], owner_token=owner_token,
            )
            return job_id
        except ArchiveChangedError as exc:
            last_error = exc
            logger.info(
                "single-entry enqueue: archive changed between preflight and enqueue for entry "
                "%s; rebuilding (attempt %d/%d)",
                entry_id, attempt + 1, _MAX_ARCHIVE_CHANGED_ATTEMPTS,
            )
            continue
    raise last_error


async def enqueue_apply_all_job(
    conn, *, persona_text: str, model: str, owner_token: str,
    activate: bool, prompt_version_id: int | None = None,
) -> tuple[int, int | None]:
    """Prepare+preflight EVERY concrete Entry Reflection request plus the Life Report request for
    the current archive, then atomically activate the prompt (if activate=True) and enqueue the
    full regeneration job in one transaction. Raises context_budget.ContextTooLargeError /
    ModelLimitsUnavailableError for the FIRST offending request (no partial job is ever prepared
    or written), db.MaintenanceLockedError, db.TargetBusyError, or (after exhausting rebuild
    attempts) db.ArchiveChangedError.

    Exactly one of activate/prompt_version_id's corresponding db.enqueue_snapshot_regen_job
    argument is used: activate=True builds targets from (persona_text, model) and activates them
    as a new persona_prompt version; activate=False reuses the given prompt_version_id (the
    already-active prompt) unchanged -- mirrors the legacy no-body vs. {draft_prompt, model}
    apply-all behavior."""
    if activate and prompt_version_id is not None:
        raise ValueError("prompt_version_id must not be given when activate=True")
    if not activate and prompt_version_id is None:
        raise ValueError("prompt_version_id is required when activate=False")

    last_error: ArchiveChangedError | None = None
    for attempt in range(_MAX_ARCHIVE_CHANGED_ATTEMPTS):
        preflight_entry_ids = get_ordered_entry_ids(conn)
        all_entries = [dict(row) for row in get_entries_in_order(conn, preflight_entry_ids)]

        # Fetch the model's current limit ONCE for the whole batch rather than once per entry —
        # get_model_max_prompt_tokens() does a real model-list round trip.
        limit = await llm.get_model_max_prompt_tokens(model)

        targets = []
        for entry in all_entries:
            envelope = llm.build_commentary_envelope(entry, all_entries, persona_text, model)
            preflight_envelope(envelope, limit)  # raises ContextTooLargeError before any write
            targets.append(PreparedRegenTarget(
                target_type="entry_commentary", entry_id=entry["id"],
                request_format_version=envelope.assembly_version,
                request_fingerprint=envelope_fingerprint(envelope),
            ))

        report_envelope = llm.build_report_envelope(all_entries, persona_text, model)
        preflight_envelope(report_envelope, limit)
        targets.append(PreparedRegenTarget(
            target_type="aggregate_report", entry_id=None,
            request_format_version=report_envelope.assembly_version,
            request_fingerprint=envelope_fingerprint(report_envelope),
        ))

        kwargs = dict(preflight_entry_ids=preflight_entry_ids, targets=targets, owner_token=owner_token)
        if activate:
            kwargs["activate_prompt"] = (persona_text, model)
        else:
            kwargs["prompt_version_id"] = prompt_version_id

        try:
            return enqueue_snapshot_regen_job(conn, **kwargs)
        except ArchiveChangedError as exc:
            last_error = exc
            logger.info(
                "apply-all enqueue: archive changed between preflight and enqueue; rebuilding "
                "(attempt %d/%d)",
                attempt + 1, _MAX_ARCHIVE_CHANGED_ATTEMPTS,
            )
            continue
    raise last_error


async def retry_job_item_with_admission(
    conn, *, item_id: int, owner_token: str, expected_job_id: int | None = None,
) -> int:
    """The deep, async retry-ADMISSION interface satisfying acceptance 811-813: retry must REFUSE
    TO RUN -- writing nothing at all -- when the reconstructed prepared request no longer matches
    the persisted assembly version/fingerprint, or the model's CURRENT published limit no longer
    admits it. This function performs every one of those checks BEFORE ever calling the atomic
    DB-side retry helper (db.retry_failed_job_item), which then re-validates everything itself
    (maintenance/done/snapshot/baseline/newer-work/single-flight/lease) under its own BEGIN
    IMMEDIATE as the final, authoritative gate.

    Order of checks (any failure raises before any DB write from THIS function; a failure inside
    the final db.retry_failed_job_item() call is itself already no-write on every path):

    1. the item exists, optionally belongs to expected_job_id (else db.ItemJobMismatchError,
       stable 404), and is 'failed' (else db.StaleOrSupersededRetryError);
    2. its owning job has a context snapshot and is 'done' (else db.StaleOrSupersededRetryError);
    3. the stored snapshot's row COUNT matches snapshot_entry_count, and every snapshotted entry
       still exists (else db.StaleOrSupersededRetryError -- never reconstruct from a truncated
       snapshot);
    4. the job's prompt version still exists (else db.StaleOrSupersededRetryError);
    5. the item's target identity is present (entry_id for entry_commentary, none for
       aggregate_report) and it resolves inside the snapshot (else
       db.StaleOrSupersededRetryError);
    6. the EXACT envelope is reconstructed from that immutable prompt + snapshot, and its
       assembly version/fingerprint must match what was stored at enqueue time (else
       db.RequestFormatChangedError, stable 409 request_format_changed);
    7. the model's CURRENT max_prompt_tokens is fetched and the reconstructed envelope is
       preflighted against it (else context_budget.ModelLimitsUnavailableError /
       ContextTooLargeError, stable 503/413).

    Only once ALL of the above succeed does this call db.retry_failed_job_item(), which performs
    the actual atomic requeue and returns the authoritative owning job_id (also returned here)."""
    item = conn.execute("SELECT * FROM regen_job_item WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        raise StaleOrSupersededRetryError(f"item {item_id} not found")
    if expected_job_id is not None and item["job_id"] != expected_job_id:
        raise ItemJobMismatchError(item_id, expected_job_id, item["job_id"])
    if item["status"] != "failed":
        raise StaleOrSupersededRetryError(f"item {item_id} not found or not in a failed state")

    job = conn.execute("SELECT * FROM regen_job WHERE id = ?", (item["job_id"],)).fetchone()
    if job is None or job["snapshot_entry_count"] is None:
        raise StaleOrSupersededRetryError(
            f"job {item['job_id']} has no context snapshot (legacy job); not retryable"
        )
    if job["status"] != "done":
        raise StaleOrSupersededRetryError(
            f"job {item['job_id']} is not done (status={job['status']!r}); not retryable"
        )

    snapshot_ids = get_job_entry_snapshot(conn, job["id"])
    if len(snapshot_ids) != job["snapshot_entry_count"]:
        raise StaleOrSupersededRetryError(
            f"job {job['id']}'s stored snapshot row count does not match snapshot_entry_count"
        )
    all_entries = [dict(row) for row in get_entries_in_order(conn, snapshot_ids)]
    if len(all_entries) != len(snapshot_ids):
        raise StaleOrSupersededRetryError(
            f"job {job['id']}'s snapshot references one or more missing diary entries"
        )

    prompt = conn.execute(
        "SELECT * FROM persona_prompt WHERE id = ?", (job["prompt_version_id"],)
    ).fetchone()
    if prompt is None:
        raise StaleOrSupersededRetryError(
            f"job {job['id']}'s prompt version {job['prompt_version_id']} no longer exists"
        )

    if item["target_type"] == "entry_commentary":
        if not item["entry_id"]:
            raise StaleOrSupersededRetryError(
                f"item {item_id} is an entry_commentary item without an entry_id"
            )
        entry = next((e for e in all_entries if e["id"] == item["entry_id"]), None)
        if entry is None:
            raise StaleOrSupersededRetryError(
                f"item {item_id}'s entry {item['entry_id']} is not in job {job['id']}'s snapshot"
            )
        envelope = llm.build_commentary_envelope(entry, all_entries, prompt["body_text"], prompt["model"])
    else:
        if item["entry_id"] is not None:
            raise StaleOrSupersededRetryError(
                f"item {item_id} is an aggregate_report item that unexpectedly carries an entry_id"
            )
        envelope = llm.build_report_envelope(all_entries, prompt["body_text"], prompt["model"])

    if not item["request_format_version"] or not item["request_fingerprint"]:
        raise StaleOrSupersededRetryError(
            f"item {item_id} has no recorded request format/fingerprint; not retryable"
        )
    if not matches_stored_assembly(
        envelope,
        stored_assembly_version=item["request_format_version"],
        stored_fingerprint=item["request_fingerprint"],
    ):
        raise RequestFormatChangedError(
            f"item {item_id}'s reconstructed request no longer matches its stored assembly "
            "version/fingerprint -- the code that assembles requests changed since this item "
            "was admitted"
        )

    limit = await llm.get_model_max_prompt_tokens(prompt["model"])
    preflight_envelope(envelope, limit)  # raises ContextTooLargeError -- no DB write yet

    return retry_failed_job_item(
        conn, item_id=item_id, owner_token=owner_token, expected_job_id=expected_job_id,
    )
