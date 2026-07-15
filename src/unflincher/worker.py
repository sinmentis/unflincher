"""In-process asyncio batch worker for 'apply to all' and single-entry background jobs. Bounded
concurrency via a semaphore; per-item failure isolation; every successful item is persisted
atomically (see db.complete_job_item) so a crash mid-item can never produce a duplicate on resume.

Reads ONLY the job's own immutable prompt version and stored, ordinal-ordered entry-ID snapshot
(see db.get_job_entry_snapshot) — NEVER a live `diary_entry` query. An entry written after this
job's snapshot was captured at enqueue time is invisible to it by construction; it is picked up by
the next generation instead. A job without a snapshot is a bug in the caller (only
db.enqueue_snapshot_regen_job and db.recover_or_cancel_running_jobs may hand this worker a job_id,
and both guarantee a snapshot) and is refused rather than silently falling back to a live query.

Before every model call, this worker reconstructs the EXACT RequestEnvelope from the immutable
prompt + stored snapshot (see llm.build_commentary_envelope/build_report_envelope), verifies it
still matches the assembly version and fingerprint recorded at enqueue time (see
request_envelope.matches_stored_assembly), and reruns context-budget preflight against the
model's CURRENT published limit. Any mismatch or failure fails just that one item for an
ORDINARY (non-recovery) run — it never silently generates from an input that was never (or no
longer) validated.

`recovering=True` (see run_job) changes that per-item isolation contract for exactly one case:
acceptance 811-813 requires startup recovery to REFUSE resuming a job outright — cancelling the
whole job with zero model calls — when a reconstructed envelope's assembly version/fingerprint no
longer matches, or the model's current limit no longer admits it, rather than letting the worker
discover this later via ordinary per-item failure isolation (which would still mark the job
'done' with some items merely 'failed'). See _validate_recovered_items."""
import asyncio
import logging

from unflincher import llm
from unflincher.context_budget import preflight_envelope
from unflincher.db import (
    RequestFormatChangedError,
    complete_job_item,
    entry_target_key,
    fail_job_item,
    get_entries_in_order,
    get_job_entry_snapshot,
    release_lease_by_target,
    report_target_key,
)
from unflincher.request_envelope import matches_stored_assembly

logger = logging.getLogger(__name__)


class BatchWorker:
    def __init__(self, conn, concurrency: int = 3):
        self.conn = conn
        self.semaphore = asyncio.Semaphore(concurrency)

    async def run_job(self, job_id: int, *, recovering: bool = False) -> None:
        """Drive one snapshot-backed regen_job to completion. Fetches the model's CURRENT
        max_prompt_tokens limit ONCE for this run (every item in one job shares one model, from
        its immutable persona_prompt version) rather than once per item.

        Setup (job/prompt/snapshot/entry validation) happens BEFORE any item is claimed. If setup
        itself fails -- missing prompt, a snapshot row count that does not match
        snapshot_entry_count, or a snapshotted entry_id that no longer resolves -- every
        pending/running item's admission-time lease for this job is released and the job is
        cancelled (its unfinished items deleted) rather than left 'running' forever with leases
        nobody will ever release.

        recovering=True (see startup recovery in app.py) additionally runs a READ-ONLY validation
        pass over every unfinished item BEFORE any is claimed or any model is called: each item's
        envelope is reconstructed from the immutable prompt + stored snapshot and its assembly
        version/fingerprint checked, and the CURRENT model limit is preflighted against it. Any
        failure there cancels the WHOLE job (leases released, unfinished items deleted) rather
        than deferring to ordinary per-item failure isolation -- acceptance 811-813 requires
        recovery to refuse outright, not let a stale/oversized item merely fail while the job is
        still marked 'done'.

        If this coroutine itself is cancelled (e.g. app shutdown) at any point -- including while
        still awaiting get_model_max_prompt_tokens, before any child task exists, or while a
        child task is still waiting on the semaphore and has not yet entered its own try/finally
        -- every child per-item task is cancelled and awaited first (so each one's own `finally`
        releases ITS lease where it can), and then EVERY remaining pending/running item's lease
        for this job is released directly from stored state (idempotent: a lease a task's own
        finally already released is simply not found again) so nothing is ever stranded. The
        job/items themselves are left exactly as they are so startup recovery can pick the job up
        again next run."""
        tasks: list[asyncio.Task] = []
        try:
            job = self.conn.execute("SELECT * FROM regen_job WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                raise ValueError(f"regen_job {job_id} does not exist")
            if job["snapshot_entry_count"] is None:
                raise ValueError(
                    f"regen_job {job_id} has no context snapshot; refusing to run it against the "
                    "live archive (see db.recover_or_cancel_running_jobs, which must never hand a "
                    "snapshot-less job to this worker)"
                )
            prompt = self.conn.execute(
                "SELECT * FROM persona_prompt WHERE id = ?", (job["prompt_version_id"],)
            ).fetchone()
            if prompt is None:
                raise ValueError(
                    f"regen_job {job_id}'s prompt version {job['prompt_version_id']} no longer exists"
                )
            prompt_version_id, persona_text, model = job["prompt_version_id"], prompt["body_text"], prompt["model"]

            ordered_entry_ids = get_job_entry_snapshot(self.conn, job_id)
            if len(ordered_entry_ids) != job["snapshot_entry_count"]:
                raise ValueError(
                    f"regen_job {job_id}'s stored snapshot has {len(ordered_entry_ids)} row(s) but "
                    f"snapshot_entry_count={job['snapshot_entry_count']}; refusing to generate from "
                    "a possibly-truncated context"
                )
            all_entries = [dict(row) for row in get_entries_in_order(self.conn, ordered_entry_ids)]
            if len(all_entries) != len(ordered_entry_ids):
                raise ValueError(
                    f"regen_job {job_id}'s snapshot references {len(ordered_entry_ids)} entr"
                    f"{'y' if len(ordered_entry_ids) == 1 else 'ies'} but only {len(all_entries)} "
                    "still exist"
                )

            try:
                current_limit = await llm.get_model_max_prompt_tokens(model)
                limit_error = None
            except Exception as exc:
                # The model's limit could not be (re)confirmed at all -- every pending/running item
                # in this job fails with the same stable reason rather than each repeating the same
                # failed model-list fetch.
                current_limit = None
                limit_error = exc

            if recovering:
                refusal_reason = self._validate_recovered_items(
                    job_id, prompt_version_id, persona_text, model, all_entries,
                    current_limit, limit_error,
                )
                if refusal_reason is not None:
                    logger.warning(
                        "startup recovery: refusing to resume regen_job %d — %s; cancelling "
                        "the job and releasing its leases rather than claiming any item",
                        job_id, refusal_reason,
                    )
                    self._cancel_setup_failure(job_id)
                    return

            while True:
                item = self._claim_next_pending(job_id)
                if item is None:
                    break
                tasks.append(asyncio.create_task(
                    self._process_item(
                        item, prompt_version_id, persona_text, model, all_entries, current_limit, limit_error
                    )
                ))
            if tasks:
                await asyncio.gather(*tasks)
            self._finalize_job(job_id)
        except asyncio.CancelledError:
            await self._cancel_and_await_tasks(tasks)
            self._release_remaining_item_leases(job_id)
            raise
        except Exception:
            await self._cancel_and_await_tasks(tasks)
            self._cancel_setup_failure(job_id)
            raise

    @staticmethod
    async def _cancel_and_await_tasks(tasks: list[asyncio.Task]) -> None:
        """Cancel every not-yet-done child task and await all of them (swallowing whatever they
        raise) so each one's own `finally` (which releases its item's lease) runs to completion
        before the caller proceeds. Used on both the cancellation and setup-failure exit paths --
        a per-item task must never be abandoned mid-flight holding a lease nobody will release."""
        if not tasks:
            return
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _release_remaining_item_leases(self, job_id: int) -> None:
        """Release every remaining pending/running item's admission-time target lease for this
        job, WITHOUT touching item or job status -- used on the plain-cancellation exit path of
        run_job, in addition to _cancel_and_await_tasks, to cover two ways a lease could
        otherwise be stranded that a child task's own try/finally can never reach:

        - run_job is cancelled before a single child task exists yet (e.g. still awaiting
          get_model_max_prompt_tokens, or during the recovery validation pass) -- there is
          nothing for _cancel_and_await_tasks to cancel, but this job's items still hold their
          admission-time leases from enqueue/recovery time.
        - a child task is cancelled while still awaiting `async with self.semaphore:` itself
          (i.e. it never finished acquiring the semaphore) -- its own try/finally body, which is
          what releases its lease, never runs, because the CancelledError fires before that
          block is even entered.

        Idempotent: release_lease_by_target is a plain DELETE, so a target whose lease a child
        task's own finally already released is simply not found again here, and this is safe to
        call even for a job_id that does not exist (its item query then returns nothing). Job and
        item status are left completely untouched so the next startup's recovery can resume this
        job exactly as if it had crashed."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            items = self.conn.execute(
                "SELECT * FROM regen_job_item WHERE job_id = ? AND status IN ('pending', 'running')",
                (job_id,),
            ).fetchall()
            for item in items:
                release_lease_by_target(self.conn, self._target_key_for(item))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _validate_recovered_items(
        self, job_id, prompt_version_id, persona_text, model, all_entries, current_limit, limit_error,
    ) -> str | None:
        """Read-only recovery-only validation pass (see run_job's `recovering` kwarg). Performs
        ZERO model calls and claims no items. Reconstructs every unfinished item's envelope from
        the immutable prompt + stored snapshot exactly as _process_item would, but returns the
        first failure reason as a string (for the caller to log and cancel the WHOLE job on)
        instead of isolating the failure to one item -- acceptance 811-813 requires recovery to
        refuse resuming outright when a reconstructed assembly/fingerprint mismatches or the
        model's current limit no longer admits a request, not merely fail that one item while the
        job is still marked 'done'. Returns None when every unfinished item passes."""
        if limit_error is not None:
            return f"model limit could not be reconfirmed for {model!r}: {limit_error}"
        items = self.conn.execute(
            "SELECT * FROM regen_job_item WHERE job_id = ? AND status IN ('pending', 'running')",
            (job_id,),
        ).fetchall()
        for item in items:
            try:
                if item["target_type"] == "entry_commentary":
                    entry = next((e for e in all_entries if e["id"] == item["entry_id"]), None)
                    if entry is None:
                        raise ValueError(
                            f"entry {item['entry_id']} is not a member of this job's own stored "
                            "snapshot"
                        )
                    envelope = llm.build_commentary_envelope(entry, all_entries, persona_text, model)
                else:
                    envelope = llm.build_report_envelope(all_entries, persona_text, model)
                self._verify_envelope_matches_item(envelope, item)
                preflight_envelope(envelope, current_limit)
            except Exception as exc:
                return f"item {item['id']} ({item['target_type']}) failed recovery validation: {exc}"
        return None

    def _cancel_setup_failure(self, job_id: int) -> None:
        """Release every still-held admission lease for this job's unfinished items, delete those
        items, and mark the job cancelled -- used when run_job's OWN setup fails (missing job,
        missing prompt, a malformed/short snapshot, or a missing entry) before any per-item
        processing could even begin. Never leaves a job 'running' forever with leases nobody will
        ever release."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            items = self.conn.execute(
                "SELECT * FROM regen_job_item WHERE job_id = ? AND status IN ('pending', 'running')",
                (job_id,),
            ).fetchall()
            for item in items:
                release_lease_by_target(self.conn, self._target_key_for(item))
            self.conn.execute(
                "DELETE FROM regen_job_item WHERE job_id = ? AND status IN ('pending', 'running')",
                (job_id,),
            )
            self.conn.execute(
                "UPDATE regen_job SET status = 'cancelled', finished_at = datetime('now') WHERE id = ?",
                (job_id,),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _claim_next_pending(self, job_id):
        row = self.conn.execute(
            "SELECT * FROM regen_job_item WHERE job_id = ? AND status = 'pending' LIMIT 1",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        self.conn.execute(
            "UPDATE regen_job_item SET status = 'running', updated_at = datetime('now') WHERE id = ?",
            (row["id"],),
        )
        return row

    def _target_key_for(self, item) -> str:
        if item["target_type"] == "entry_commentary":
            return entry_target_key(item["entry_id"])
        return report_target_key()

    async def _process_item(self, item, prompt_version_id, persona_text, model, all_entries, current_limit, limit_error):
        async with self.semaphore:
            try:
                if limit_error is not None:
                    raise limit_error
                if item["target_type"] == "entry_commentary":
                    await self._generate_entry_commentary(
                        item, prompt_version_id, persona_text, model, all_entries, current_limit
                    )
                else:
                    await self._generate_aggregate_report(
                        item, prompt_version_id, persona_text, model, all_entries, current_limit
                    )
            except Exception as exc:
                # Deliberately broad: per-item failure isolation is this worker's OWN contract
                # (see module docstring), distinct from stream_completion_envelope's narrow
                # transport-retry catch -- any failure here (context-too-large, model-limits-
                # unavailable, request-format-changed, model/transport error) fails just this item.
                fail_job_item(self.conn, item["id"], str(exc))
            finally:
                # Always release this item's admission-time exclusive lease, regardless of
                # success or failure, so a later retry/new generation for the same target is never
                # stranded behind a lease this worker forgot to release.
                release_lease_by_target(self.conn, self._target_key_for(item))

    def _verify_envelope_matches_item(self, envelope, item) -> None:
        if not item["request_format_version"] or not item["request_fingerprint"]:
            raise RequestFormatChangedError(
                f"regen_job_item {item['id']} has no recorded request format/fingerprint"
            )
        if not matches_stored_assembly(
            envelope,
            stored_assembly_version=item["request_format_version"],
            stored_fingerprint=item["request_fingerprint"],
        ):
            raise RequestFormatChangedError(
                f"regen_job_item {item['id']}'s reconstructed request no longer matches its "
                "stored assembly version/fingerprint -- the code that assembles requests changed "
                "since this item was admitted"
            )

    async def _generate_entry_commentary(self, item, prompt_version_id, persona_text, model, all_entries, current_limit):
        entry = next(e for e in all_entries if e["id"] == item["entry_id"])
        envelope = llm.build_commentary_envelope(entry, all_entries, persona_text, model)
        self._verify_envelope_matches_item(envelope, item)
        preflight_envelope(envelope, current_limit)
        chunks = [tok async for tok in llm.stream_completion_envelope(envelope)]
        complete_job_item(self.conn, item["id"], "entry_commentary", {
            "entry_id": item["entry_id"], "prompt_version_id": prompt_version_id,
            "model": model, "body_text": "".join(chunks), "status": "ok",
        })

    async def _generate_aggregate_report(self, item, prompt_version_id, persona_text, model, all_entries, current_limit):
        envelope = llm.build_report_envelope(all_entries, persona_text, model)
        self._verify_envelope_matches_item(envelope, item)
        preflight_envelope(envelope, current_limit)
        chunks = [tok async for tok in llm.stream_completion_envelope(envelope)]
        dates = [e["entry_date"] for e in all_entries]
        complete_job_item(self.conn, item["id"], "aggregate_report", {
            "prompt_version_id": prompt_version_id, "model": model, "body_text": "".join(chunks),
            "covered_entry_count": len(all_entries),
            "covered_from_date": min(dates) if dates else None,
            "covered_to_date": max(dates) if dates else None,
            "status": "ok",
        })

    def _finalize_job(self, job_id):
        remaining = self.conn.execute(
            "SELECT COUNT(*) AS n FROM regen_job_item WHERE job_id = ? AND status IN ('pending','running')",
            (job_id,),
        ).fetchone()["n"]
        if remaining == 0:
            self.conn.execute(
                "UPDATE regen_job SET status = 'done', finished_at = datetime('now') WHERE id = ?",
                (job_id,),
            )
