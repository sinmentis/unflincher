import asyncio

import pytest

import unflincher.llm as llm_module
from unflincher.db import (
    PreparedRegenTarget,
    enqueue_snapshot_regen_job,
    get_connection,
    get_current_commentary,
    init_schema,
    migrate_generation_safety,
    migrate_persona_prompt_model,
    set_active_prompt,
)
from unflincher.request_envelope import fingerprint as envelope_fingerprint
from unflincher.worker import BatchWorker


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.db"))
    init_schema(c)
    migrate_persona_prompt_model(c)
    migrate_generation_safety(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _fake_model_limit(monkeypatch):
    """Every worker test needs get_model_max_prompt_tokens() to succeed without a real Copilot
    client -- the worker fetches it once per run_job() call to rerun current-limit preflight."""
    async def _fake_limit(model):
        return 200_000
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _fake_limit)


def _seed_entries(conn, n):
    return [
        conn.execute(
            "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
            "entry_date, source) VALUES (?, '<p>x</p>', '<p>x</p>', 'x', ?, 'import')",
            (f"日记{i}", f"2026-01-{i+1:02d}"),
        ).lastrowid
        for i in range(n)
    ]


def _enqueue_full_job(conn, entry_ids, persona_text="人设", model="test-model"):
    """Seed a real snapshot-backed job the same way regen_enqueue/routes would: one
    entry_commentary target per entry plus one aggregate_report target, each with the EXACT
    fingerprint the worker will reconstruct via llm.build_commentary_envelope/build_report_envelope
    for the given persona_text/model -- so worker's fingerprint verification succeeds exactly as
    it would for a job created through the real enqueue path."""
    prompt_id = set_active_prompt(conn, persona_text, model)
    all_entries = [dict(conn.execute("SELECT * FROM diary_entry WHERE id = ?", (eid,)).fetchone()) for eid in entry_ids]
    targets = []
    for entry in all_entries:
        envelope = llm_module.build_commentary_envelope(entry, all_entries, persona_text, model)
        targets.append(PreparedRegenTarget(
            "entry_commentary", entry["id"], envelope.assembly_version, envelope_fingerprint(envelope)
        ))
    report_envelope = llm_module.build_report_envelope(all_entries, persona_text, model)
    targets.append(PreparedRegenTarget(
        "aggregate_report", None, report_envelope.assembly_version, envelope_fingerprint(report_envelope)
    ))
    job_id, _ = enqueue_snapshot_regen_job(
        conn, prompt_version_id=prompt_id, preflight_entry_ids=entry_ids, targets=targets,
        owner_token="test-worker",
    )
    return job_id, prompt_id


async def test_run_job_generates_commentary_for_every_entry_and_the_report(monkeypatch, conn):
    async def fake_stream(envelope):
        if envelope.target_kind == "entry_commentary":
            yield f'关于条目{envelope.target_id}的锐评\n\n[wellbeing-score]: # "73"'
        else:
            yield "汇总报告"

    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)

    entry_ids = _seed_entries(conn, 3)
    job_id, _ = _enqueue_full_job(conn, entry_ids)

    worker = BatchWorker(conn, concurrency=2)
    await worker.run_job(job_id)

    for entry_id in entry_ids:
        current = get_current_commentary(conn, entry_id)
        assert current is not None
        assert "锐评" in current["body_text"]
    report = conn.execute("SELECT * FROM aggregate_report WHERE status='ok'").fetchone()
    assert report["body_text"] == "汇总报告"
    assert report["covered_entry_count"] == 3

    job = conn.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "done"


async def test_run_job_releases_every_target_lease_on_success(monkeypatch, conn):
    from unflincher.db import entry_target_key, get_lease_by_target, report_target_key

    async def fake_stream(envelope):
        yield "ok"

    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)

    entry_ids = _seed_entries(conn, 2)
    job_id, _ = _enqueue_full_job(conn, entry_ids)

    # Leases were acquired at enqueue time.
    assert get_lease_by_target(conn, entry_target_key(entry_ids[0])) is not None
    assert get_lease_by_target(conn, report_target_key()) is not None

    worker = BatchWorker(conn, concurrency=2)
    await worker.run_job(job_id)

    for entry_id in entry_ids:
        assert get_lease_by_target(conn, entry_target_key(entry_id)) is None
    assert get_lease_by_target(conn, report_target_key()) is None


async def test_run_job_isolates_failures_and_releases_lease_even_on_failure(monkeypatch, conn):
    from unflincher.db import entry_target_key, get_lease_by_target

    async def fake_stream(envelope):
        if envelope.target_kind == "entry_commentary" and envelope.target_id == "1":
            raise RuntimeError("provider error")
        if envelope.target_kind == "entry_commentary":
            yield 'ok take\n\n[wellbeing-score]: # "73"'
        else:
            yield "ok report"

    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)

    entry_ids = _seed_entries(conn, 2)
    job_id, _ = _enqueue_full_job(conn, entry_ids)

    worker = BatchWorker(conn, concurrency=2)
    await worker.run_job(job_id)

    failed_item = conn.execute(
        "SELECT * FROM regen_job_item WHERE job_id = ? AND entry_id = ?", (job_id, entry_ids[0]),
    ).fetchone()
    assert failed_item["status"] == "failed"
    assert "provider error" in failed_item["error"]
    ok_item = conn.execute(
        "SELECT * FROM regen_job_item WHERE job_id = ? AND entry_id = ?", (job_id, entry_ids[1]),
    ).fetchone()
    assert ok_item["status"] == "ok"
    # job still reaches 'done' — done means "finished", not "all succeeded"
    job = conn.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "done"
    # The failed item's lease was still released -- failure isolation must never strand a lease.
    assert get_lease_by_target(conn, entry_target_key(entry_ids[0])) is None


async def test_run_job_refuses_a_job_without_a_context_snapshot(conn):
    prompt_id = set_active_prompt(conn, "人设", "test-model")
    job_id = conn.execute(
        "INSERT INTO regen_job (prompt_version_id, status) VALUES (?, 'running')", (prompt_id,)
    ).lastrowid  # no snapshot_entry_count -> legacy, must never be handed to the worker

    worker = BatchWorker(conn, concurrency=1)
    with pytest.raises(ValueError, match="context snapshot"):
        await worker.run_job(job_id)


async def test_run_job_uses_only_the_stored_snapshot_not_live_archive(monkeypatch, conn):
    # An entry inserted AFTER this job's snapshot was captured must never appear in its context,
    # even though it now exists in the live diary_entry table.
    seen_user_contents = []

    async def fake_stream(envelope):
        seen_user_contents.append(envelope.user_content)
        yield "report"

    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)

    entry_ids = _seed_entries(conn, 2)
    job_id, _ = _enqueue_full_job(conn, entry_ids)

    # Entry written AFTER enqueue -- must be invisible to this job.
    conn.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('迟到的日记', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    )

    worker = BatchWorker(conn, concurrency=2)
    await worker.run_job(job_id)

    assert all("迟到的日记" not in c for c in seen_user_contents)


async def test_run_job_fails_item_when_reconstructed_request_no_longer_matches_stored_fingerprint(monkeypatch, conn):
    entry_ids = _seed_entries(conn, 1)
    job_id, _ = _enqueue_full_job(conn, entry_ids)
    # Corrupt the stored fingerprint as if the code that assembles requests changed since
    # admission (or the row was tampered with) -- the item must fail, never generate.
    conn.execute(
        "UPDATE regen_job_item SET request_fingerprint = 'stale-fingerprint' "
        "WHERE job_id = ? AND target_type = 'entry_commentary'",
        (job_id,),
    )

    called = {"n": 0}

    async def fake_stream(envelope):
        called["n"] += 1
        yield "should not run"

    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)

    worker = BatchWorker(conn, concurrency=2)
    await worker.run_job(job_id)

    item = conn.execute(
        "SELECT * FROM regen_job_item WHERE job_id = ? AND target_type = 'entry_commentary'", (job_id,)
    ).fetchone()
    assert item["status"] == "failed"
    assert "no longer matches" in item["error"]


async def test_run_job_fails_all_items_when_model_limit_is_unavailable(monkeypatch, conn):
    from unflincher.context_budget import ModelLimitsUnavailableError

    async def _raise(model):
        raise ModelLimitsUnavailableError(model, "model list unavailable")
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _raise)

    called = {"n": 0}

    async def fake_stream(envelope):
        called["n"] += 1
        yield "should not run"
    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)

    entry_ids = _seed_entries(conn, 1)
    job_id, _ = _enqueue_full_job(conn, entry_ids)

    worker = BatchWorker(conn, concurrency=2)
    await worker.run_job(job_id)

    assert called["n"] == 0  # never called the model
    items = conn.execute("SELECT * FROM regen_job_item WHERE job_id = ?", (job_id,)).fetchall()
    assert all(i["status"] == "failed" for i in items)


async def test_run_job_fails_item_when_context_too_large(monkeypatch, conn):
    async def _fake_limit(model):
        return 1  # tiny limit -- any real content overflows it
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _fake_limit)

    async def fake_stream(envelope):
        yield "should not run"
    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)

    entry_ids = _seed_entries(conn, 1)
    job_id, _ = _enqueue_full_job(conn, entry_ids)

    worker = BatchWorker(conn, concurrency=2)
    await worker.run_job(job_id)

    items = conn.execute("SELECT * FROM regen_job_item WHERE job_id = ?", (job_id,)).fetchall()
    assert all(i["status"] == "failed" for i in items)


async def test_run_job_requeued_item_after_crash_produces_exactly_one_result(monkeypatch, conn):
    """Simulates a hard crash: an item stuck 'running' with no result yet (the process died
    before the LLM call returned). Re-running the worker after resetting it back to 'pending'
    must produce exactly one commentary row, not two."""
    async def fake_stream(envelope):
        if envelope.target_kind == "entry_commentary":
            yield '重新生成的锐评\n\n[wellbeing-score]: # "73"'
        else:
            yield "重新生成的报告"
    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)

    entry_ids = _seed_entries(conn, 1)
    job_id, _ = _enqueue_full_job(conn, entry_ids)
    # Simulate the crash: mark the report item 'ok' (so we only re-drive the entry item) and
    # force the entry item back to 'pending' with no result_id, as if the worker claimed it but
    # died before calling complete_job_item (recover_or_cancel_running_jobs would normally do this
    # reset; this test drives BatchWorker directly, so it's done inline here).
    conn.execute(
        "UPDATE regen_job_item SET status='ok' WHERE job_id=? AND target_type='aggregate_report'",
        (job_id,),
    )
    conn.execute(
        "UPDATE regen_job_item SET status='pending' WHERE job_id=? AND target_type='entry_commentary'",
        (job_id,),
    )

    worker = BatchWorker(conn, concurrency=1)
    await worker.run_job(job_id)

    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM entry_commentary WHERE entry_id = ?", (entry_ids[0],)
    ).fetchone()
    assert rows["n"] == 1


async def test_run_job_setup_failure_missing_prompt_cancels_job_and_releases_leases(monkeypatch, conn):
    from unflincher.db import entry_target_key, get_lease_by_target, report_target_key

    entry_ids = _seed_entries(conn, 1)
    job_id, prompt_id = _enqueue_full_job(conn, entry_ids)
    # Simulate corruption: the job's own prompt version row disappears. Bypass FK
    # enforcement, since this app's own code never actually deletes a prompt version --
    # this test proves worker-level defense-in-depth against a scenario that "should never
    # happen" through normal app code paths.
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM persona_prompt WHERE id = ?", (prompt_id,))
    conn.execute("PRAGMA foreign_keys=ON")

    worker = BatchWorker(conn, concurrency=1)
    with pytest.raises(ValueError, match="prompt version"):
        await worker.run_job(job_id)

    job = conn.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "cancelled"
    remaining_items = conn.execute(
        "SELECT COUNT(*) AS n FROM regen_job_item WHERE job_id = ?", (job_id,)
    ).fetchone()["n"]
    assert remaining_items == 0
    assert get_lease_by_target(conn, entry_target_key(entry_ids[0])) is None
    assert get_lease_by_target(conn, report_target_key()) is None


async def test_run_job_setup_failure_snapshot_count_mismatch_cancels_job_and_releases_leases(monkeypatch, conn):
    from unflincher.db import entry_target_key, get_lease_by_target, report_target_key

    entry_ids = _seed_entries(conn, 1)
    job_id, _ = _enqueue_full_job(conn, entry_ids)
    # Corrupt the stored snapshot: delete its one row while snapshot_entry_count still says 1.
    conn.execute("DELETE FROM regen_job_entry_snapshot WHERE job_id = ?", (job_id,))

    worker = BatchWorker(conn, concurrency=1)
    with pytest.raises(ValueError, match="snapshot"):
        await worker.run_job(job_id)

    job = conn.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "cancelled"
    assert get_lease_by_target(conn, entry_target_key(entry_ids[0])) is None
    assert get_lease_by_target(conn, report_target_key()) is None


async def test_run_job_setup_failure_missing_entry_cancels_job_and_releases_leases(monkeypatch, conn):
    from unflincher.db import entry_target_key, get_lease_by_target, report_target_key

    entry_ids = _seed_entries(conn, 1)
    job_id, _ = _enqueue_full_job(conn, entry_ids)
    # Simulate corruption: the snapshotted entry no longer exists (this app never actually
    # deletes entries, but a defensive check must still catch this). Bypass FK enforcement to
    # reach this "should never happen" state.
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM diary_entry WHERE id = ?", (entry_ids[0],))
    conn.execute("PRAGMA foreign_keys=ON")

    worker = BatchWorker(conn, concurrency=1)
    with pytest.raises(ValueError, match="still exist"):
        await worker.run_job(job_id)

    job = conn.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "cancelled"
    assert get_lease_by_target(conn, entry_target_key(entry_ids[0])) is None
    assert get_lease_by_target(conn, report_target_key()) is None


async def test_run_job_cancellation_releases_leases_and_does_not_mark_job_cancelled(monkeypatch, conn):
    """Simulates the app-shutdown scenario: run_job() itself is cancelled while an item is
    actively streaming. The in-flight per-item task must be cancelled and awaited (releasing its
    lease) before the cancellation propagates, but the job/items are left as-is (NOT force-
    cancelled) so startup recovery can resume it on the next run."""
    from unflincher.db import entry_target_key, get_lease_by_target, report_target_key

    started = asyncio.Event()
    may_finish = asyncio.Event()

    async def fake_stream(envelope):
        started.set()
        await may_finish.wait()
        yield "should not get here before cancellation"

    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)

    entry_ids = _seed_entries(conn, 1)
    job_id, _ = _enqueue_full_job(conn, entry_ids)

    worker = BatchWorker(conn, concurrency=2)
    run_task = asyncio.create_task(worker.run_job(job_id))
    await started.wait()  # at least one item is now actively "streaming"

    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    # The job was NOT force-marked cancelled -- it stays exactly as a crash would leave it, for
    # startup recovery to pick back up.
    job = conn.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "running"
    # Every lease this run had acquired admission for was released by the cancelled child task's
    # own `finally` -- nothing is left stranded.
    assert get_lease_by_target(conn, entry_target_key(entry_ids[0])) is None
    assert get_lease_by_target(conn, report_target_key()) is None


async def test_run_job_cancellation_during_model_limit_lookup_releases_leases(monkeypatch, conn):
    """Regression test: run_job cancelled while STILL awaiting get_model_max_prompt_tokens --
    before a single child task exists -- must still release every item's admission-time lease.
    _cancel_and_await_tasks alone cannot do this (there is nothing to cancel yet); the fix is the
    separate _release_remaining_item_leases call on the cancellation exit path."""
    from unflincher.db import entry_target_key, get_lease_by_target, report_target_key

    entered_limit_lookup = asyncio.Event()
    may_finish = asyncio.Event()

    async def hanging_limit(model):
        entered_limit_lookup.set()
        await may_finish.wait()
        return 200_000

    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", hanging_limit)

    entry_ids = _seed_entries(conn, 1)
    job_id, _ = _enqueue_full_job(conn, entry_ids)

    worker = BatchWorker(conn, concurrency=2)
    run_task = asyncio.create_task(worker.run_job(job_id))
    await entered_limit_lookup.wait()

    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    job = conn.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "running"  # left as-is for the next startup's recovery, not cancelled
    assert get_lease_by_target(conn, entry_target_key(entry_ids[0])) is None
    assert get_lease_by_target(conn, report_target_key()) is None


async def test_run_job_cancellation_while_other_items_wait_on_the_semaphore_releases_their_leases(monkeypatch, conn):
    """Regression test: with more items than concurrency, the item(s) still awaiting `async with
    self.semaphore:` -- never having entered their own try/finally -- must still have their lease
    released when run_job is cancelled. Their own finally never runs because CancelledError fires
    at the semaphore acquire itself, before the try block is ever entered."""
    from unflincher.db import entry_target_key, get_lease_by_target, report_target_key

    started = asyncio.Event()
    may_finish = asyncio.Event()

    async def fake_stream(envelope):
        started.set()
        await may_finish.wait()
        yield "should not get here before cancellation"

    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)

    entry_ids = _seed_entries(conn, 2)  # 2 entry_commentary + 1 aggregate_report = 3 items
    job_id, _ = _enqueue_full_job(conn, entry_ids)

    worker = BatchWorker(conn, concurrency=1)  # only one item can be "inside" processing at once
    run_task = asyncio.create_task(worker.run_job(job_id))
    await started.wait()  # exactly one item acquired the semaphore and is now "streaming"
    # Give the event loop a couple of turns so the other two items' tasks actually reach (and
    # block on) `await self.semaphore.acquire()` rather than merely being scheduled.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    job = conn.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "running"
    assert get_lease_by_target(conn, entry_target_key(entry_ids[0])) is None
    assert get_lease_by_target(conn, entry_target_key(entry_ids[1])) is None
    assert get_lease_by_target(conn, report_target_key()) is None


# ---------------------------------------------------------------------------
# Recovery-only validation phase (recovering=True) -- acceptance 811-813: recovery must REFUSE
# to resume a job outright (cancel it, zero model calls) rather than let ordinary per-item
# failure isolation discover a stale/oversized item later.
# ---------------------------------------------------------------------------

async def test_run_job_recovering_refuses_job_on_fingerprint_mismatch_with_zero_model_calls(monkeypatch, conn):
    from unflincher.db import entry_target_key, get_lease_by_target, report_target_key

    entry_ids = _seed_entries(conn, 1)
    job_id, _ = _enqueue_full_job(conn, entry_ids)
    # Corrupt the stored fingerprint as if the code that assembles requests changed since this
    # job was admitted (or the row was tampered with).
    conn.execute(
        "UPDATE regen_job_item SET status = 'running', request_fingerprint = 'stale-fingerprint' "
        "WHERE job_id = ? AND target_type = 'entry_commentary'",
        (job_id,),
    )

    called = {"n": 0}

    async def fake_stream(envelope):
        called["n"] += 1
        yield "should never run"

    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)

    worker = BatchWorker(conn, concurrency=2)
    await worker.run_job(job_id, recovering=True)

    assert called["n"] == 0  # zero model calls
    job = conn.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "cancelled"  # refused outright, never left 'done' with a failed item
    remaining_items = conn.execute(
        "SELECT COUNT(*) AS n FROM regen_job_item WHERE job_id = ?", (job_id,)
    ).fetchone()["n"]
    assert remaining_items == 0
    assert get_lease_by_target(conn, entry_target_key(entry_ids[0])) is None
    assert get_lease_by_target(conn, report_target_key()) is None


async def test_run_job_recovering_refuses_job_on_assembly_version_mismatch_with_zero_model_calls(monkeypatch, conn):
    from unflincher.db import entry_target_key, get_lease_by_target, report_target_key

    entry_ids = _seed_entries(conn, 1)
    job_id, _ = _enqueue_full_job(conn, entry_ids)
    # Corrupt the stored assembly version as if request_envelope.ASSEMBLY_VERSION was bumped
    # since this job was admitted.
    conn.execute(
        "UPDATE regen_job_item SET status = 'running', request_format_version = 999 "
        "WHERE job_id = ? AND target_type = 'entry_commentary'",
        (job_id,),
    )

    called = {"n": 0}

    async def fake_stream(envelope):
        called["n"] += 1
        yield "should never run"

    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)

    worker = BatchWorker(conn, concurrency=2)
    await worker.run_job(job_id, recovering=True)

    assert called["n"] == 0
    job = conn.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "cancelled"
    assert get_lease_by_target(conn, entry_target_key(entry_ids[0])) is None
    assert get_lease_by_target(conn, report_target_key()) is None


async def test_run_job_recovering_refuses_job_when_model_limit_is_unavailable_with_zero_model_calls(monkeypatch, conn):
    from unflincher.context_budget import ModelLimitsUnavailableError
    from unflincher.db import entry_target_key, get_lease_by_target, report_target_key

    async def _raise(model):
        raise ModelLimitsUnavailableError(model, "model list unavailable")
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _raise)

    called = {"n": 0}

    async def fake_stream(envelope):
        called["n"] += 1
        yield "should never run"
    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)

    entry_ids = _seed_entries(conn, 1)
    job_id, _ = _enqueue_full_job(conn, entry_ids)
    conn.execute("UPDATE regen_job_item SET status = 'running' WHERE job_id = ?", (job_id,))

    worker = BatchWorker(conn, concurrency=2)
    await worker.run_job(job_id, recovering=True)

    assert called["n"] == 0
    job = conn.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "cancelled"
    assert get_lease_by_target(conn, entry_target_key(entry_ids[0])) is None
    assert get_lease_by_target(conn, report_target_key()) is None


async def test_run_job_recovering_refuses_job_when_context_is_now_too_large_with_zero_model_calls(monkeypatch, conn):
    from unflincher.db import entry_target_key, get_lease_by_target, report_target_key

    async def _tiny_limit(model):
        return 1  # any real content overflows this
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _tiny_limit)

    called = {"n": 0}

    async def fake_stream(envelope):
        called["n"] += 1
        yield "should never run"
    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)

    entry_ids = _seed_entries(conn, 1)
    job_id, _ = _enqueue_full_job(conn, entry_ids)
    conn.execute("UPDATE regen_job_item SET status = 'running' WHERE job_id = ?", (job_id,))

    worker = BatchWorker(conn, concurrency=2)
    await worker.run_job(job_id, recovering=True)

    assert called["n"] == 0
    job = conn.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "cancelled"
    assert get_lease_by_target(conn, entry_target_key(entry_ids[0])) is None
    assert get_lease_by_target(conn, report_target_key()) is None


async def test_run_job_recovering_proceeds_normally_when_everything_is_valid(monkeypatch, conn):
    """Sanity check: recovering=True must not refuse a perfectly valid job -- it should generate
    exactly as the non-recovery path would."""
    async def fake_stream(envelope):
        if envelope.target_kind == "entry_commentary":
            yield '重新生成的锐评\n\n[wellbeing-score]: # "73"'
        else:
            yield "重新生成的报告"
    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)

    entry_ids = _seed_entries(conn, 1)
    job_id, _ = _enqueue_full_job(conn, entry_ids)

    worker = BatchWorker(conn, concurrency=2)
    await worker.run_job(job_id, recovering=True)

    job = conn.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "done"
    current = get_current_commentary(conn, entry_ids[0])
    assert current is not None
    assert "锐评" in current["body_text"]


async def test_run_job_rejects_entry_reflection_without_score_metadata(monkeypatch, conn):
    attempts = 0

    async def fake_stream(envelope):
        nonlocal attempts
        if envelope.target_kind == "entry_commentary":
            attempts += 1
            yield "Reflection without metadata"
        else:
            yield "Report"

    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)
    entry_ids = _seed_entries(conn, 1)
    job_id, _ = _enqueue_full_job(conn, entry_ids)

    await BatchWorker(conn, concurrency=1).run_job(job_id)

    item = conn.execute(
        "SELECT status, error FROM regen_job_item "
        "WHERE job_id = ? AND target_type = 'entry_commentary'",
        (job_id,),
    ).fetchone()
    assert item["status"] == "failed"
    assert "missing wellbeing score metadata" in item["error"]
    assert get_current_commentary(conn, entry_ids[0]) is None
    assert attempts == 2


async def test_run_job_retries_invalid_entry_reflection_format_once(monkeypatch, conn):
    attempts = 0

    async def fake_stream(envelope):
        nonlocal attempts
        if envelope.target_kind == "entry_commentary":
            attempts += 1
            if attempts == 1:
                yield "Reflection without metadata"
            else:
                yield 'Recovered reflection.\n\n[wellbeing-score]: # "73"'
        else:
            yield "Report"

    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)
    entry_ids = _seed_entries(conn, 1)
    job_id, _ = _enqueue_full_job(conn, entry_ids)

    await BatchWorker(conn, concurrency=1).run_job(job_id)

    item = conn.execute(
        "SELECT status, error FROM regen_job_item "
        "WHERE job_id = ? AND target_type = 'entry_commentary'",
        (job_id,),
    ).fetchone()
    assert item["status"] == "ok"
    assert item["error"] is None
    assert get_current_commentary(conn, entry_ids[0])["body_text"].startswith(
        "Recovered reflection."
    )
    assert attempts == 2
