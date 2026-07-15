"""Startup crash-recovery integration test.

Drives create_app()'s lifespan directly (no TestClient) so we can pre-seed a crashed job on
disk BEFORE the lifespan runs, then assert on the resulting recovery/cancellation behavior —
proving both paths of db.recover_or_cancel_running_jobs() end-to-end at app startup:
- a snapshot-backed job is resumed (worker relaunched, exactly as before this workstream);
- a snapshot-LESS legacy job is cancelled and its unfinished items deleted, never resumed
  against the live archive (see the plan's Maintenance gate / archive-snapshot sections).
"""
import unflincher.llm as llm_module
from unflincher.app import create_app
from unflincher.db import (
    PreparedRegenTarget,
    enqueue_snapshot_regen_job,
    get_connection,
    init_schema,
    migrate_generation_safety,
    migrate_persona_prompt_model,
)
from unflincher.request_envelope import fingerprint as envelope_fingerprint


async def test_startup_recovers_snapshot_backed_crashed_running_job(tmp_path, monkeypatch):
    db_path = str(tmp_path / "recovery.db")

    # Seed the DB BEFORE create_app() runs its lifespan: a snapshot-backed job left mid-crash
    # (its entry_commentary item stuck 'running') plus its already-finished aggregate_report
    # item. Use a throwaway connection, then close it.
    seed = get_connection(db_path)
    init_schema(seed)
    migrate_persona_prompt_model(seed)
    migrate_generation_safety(seed)
    entry_row_id = seed.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('日记0', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    # Seed a NON-default model (settings.llm_model defaults to claude-sonnet-4.6) so the assertion
    # below proves the recovered worker uses the job's OWN persona model, not the env default.
    persona_text, model = "人设", "gpt-5.4"
    prompt_id = seed.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) VALUES (1, ?, ?, 1)",
        (persona_text, model),
    ).lastrowid
    entry_row = dict(seed.execute("SELECT * FROM diary_entry WHERE id = ?", (entry_row_id,)).fetchone())
    all_entries = [entry_row]
    envelope = llm_module.build_commentary_envelope(entry_row, all_entries, persona_text, model)
    job_id, _ = enqueue_snapshot_regen_job(
        seed, prompt_version_id=prompt_id, preflight_entry_ids=[entry_row_id],
        targets=[PreparedRegenTarget(
            "entry_commentary", entry_row_id, envelope.assembly_version, envelope_fingerprint(envelope)
        )],
        owner_token="crashed-process",
    )
    item_id = seed.execute(
        "SELECT id FROM regen_job_item WHERE job_id = ? AND target_type = 'entry_commentary'",
        (job_id,),
    ).fetchone()["id"]
    seed.execute("UPDATE regen_job_item SET status = 'running' WHERE id = ?", (item_id,))
    seed.close()

    # Module-reference monkeypatch (the worker calls llm.stream_completion_envelope) so no real
    # LLM hit.
    async def _fake_stream(envelope):
        yield "锐评：崩溃后恢复生成"
    monkeypatch.setattr(llm_module, "stream_completion_envelope", _fake_stream)

    async def _fake_limit(model):
        return 200_000
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _fake_limit)

    async def _noop(): pass
    monkeypatch.setattr(llm_module, "warm_up_client", _noop)
    monkeypatch.setattr(llm_module, "shutdown_client", _noop)
    monkeypatch.setenv("UNFLINCHER_DB", db_path)
    monkeypatch.setenv("UNFLINCHER_REQUIRE_ACCESS_AUTH", "false")

    app = create_app()
    async with app.router.lifespan_context(app):
        # The lifespan detected the snapshot-backed 'running' job and relaunched the worker;
        # await its task to force deterministic completion before asserting on DB state.
        await app.state.recovery_task
        db = app.state.db
        job = db.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
        commentaries = db.execute(
            "SELECT status, model FROM entry_commentary WHERE entry_id = ?", (entry_row_id,)
        ).fetchall()

    assert job["status"] == "done"
    # Exactly one 'ok' row — the crash-safety property: resume never duplicates a result.
    assert len(commentaries) == 1
    assert commentaries[0]["status"] == "ok"
    # The recovered worker recorded the job's own persona model, not settings.llm_model.
    assert commentaries[0]["model"] == "gpt-5.4"


async def test_startup_recovery_refuses_a_job_whose_reconstructed_fingerprint_no_longer_matches(tmp_path, monkeypatch):
    """Acceptance 811-813, end to end through the real lifespan: a job that structurally passes
    db.recover_or_cancel_running_jobs()'s checks (valid identity, valid snapshot) but whose
    reconstructed envelope fingerprint no longer matches what was persisted at enqueue time must
    be REFUSED outright by the recovery-only validation phase (BatchWorker.run_job(recovering=
    True)) -- cancelled, zero model calls -- never resumed and left to fail one item later while
    the job is marked 'done'."""
    db_path = str(tmp_path / "recovery-fingerprint-mismatch.db")

    seed = get_connection(db_path)
    init_schema(seed)
    migrate_persona_prompt_model(seed)
    migrate_generation_safety(seed)
    entry_row_id = seed.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('日记0', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    persona_text, model = "人设", "gpt-5.4"
    prompt_id = seed.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) VALUES (1, ?, ?, 1)",
        (persona_text, model),
    ).lastrowid
    entry_row = dict(seed.execute("SELECT * FROM diary_entry WHERE id = ?", (entry_row_id,)).fetchone())
    envelope = llm_module.build_commentary_envelope(entry_row, [entry_row], persona_text, model)
    job_id, _ = enqueue_snapshot_regen_job(
        seed, prompt_version_id=prompt_id, preflight_entry_ids=[entry_row_id],
        targets=[PreparedRegenTarget(
            "entry_commentary", entry_row_id, envelope.assembly_version, envelope_fingerprint(envelope)
        )],
        owner_token="crashed-process",
    )
    item_id = seed.execute(
        "SELECT id FROM regen_job_item WHERE job_id = ? AND target_type = 'entry_commentary'",
        (job_id,),
    ).fetchone()["id"]
    seed.execute("UPDATE regen_job_item SET status = 'running' WHERE id = ?", (item_id,))
    # Corrupt the stored fingerprint AFTER admission -- as if request assembly code changed since.
    seed.execute(
        "UPDATE regen_job_item SET request_fingerprint = 'stale-fingerprint-from-old-code' "
        "WHERE id = ?",
        (item_id,),
    )
    seed.close()

    called = {"n": 0}

    async def _fake_stream(envelope):
        called["n"] += 1
        yield "should never be called"
    monkeypatch.setattr(llm_module, "stream_completion_envelope", _fake_stream)

    async def _fake_limit(model):
        return 200_000
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _fake_limit)

    async def _noop(): pass
    monkeypatch.setattr(llm_module, "warm_up_client", _noop)
    monkeypatch.setattr(llm_module, "shutdown_client", _noop)
    monkeypatch.setenv("UNFLINCHER_DB", db_path)
    monkeypatch.setenv("UNFLINCHER_REQUIRE_ACCESS_AUTH", "false")

    app = create_app()
    async with app.router.lifespan_context(app):
        await app.state.recovery_task
        db = app.state.db
        job = db.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
        remaining_items = db.execute(
            "SELECT COUNT(*) AS n FROM regen_job_item WHERE job_id = ?", (job_id,)
        ).fetchone()["n"]
        commentary_count = db.execute(
            "SELECT COUNT(*) AS n FROM entry_commentary WHERE entry_id = ?", (entry_row_id,)
        ).fetchone()["n"]
        from unflincher.db import entry_target_key, get_lease_by_target
        lease = get_lease_by_target(db, entry_target_key(entry_row_id))

    assert called["n"] == 0  # zero model calls -- refused before ever generating
    assert job["status"] == "cancelled"  # refused outright, never resumed as 'done'
    assert remaining_items == 0  # unfinished item deleted, not requeued for a future retry
    assert commentary_count == 0  # never generated from the mismatched request
    assert lease is None  # the reacquired lease was released, not stranded


async def test_lifespan_shutdown_settles_in_flight_recovered_job_before_client_and_db_teardown(tmp_path, monkeypatch):
    """If the recovered worker is STILL actively streaming when the app shuts down, the lifespan
    must cancel and await it (releasing its lease) BEFORE tearing down the shared Copilot client
    or closing the database connection -- never leave a recovery task touching either after they
    are gone."""
    import asyncio

    db_path = str(tmp_path / "shutdown-recovery.db")

    seed = get_connection(db_path)
    init_schema(seed)
    migrate_persona_prompt_model(seed)
    migrate_generation_safety(seed)
    entry_row_id = seed.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('日记0', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    persona_text, model = "人设", "gpt-5.4"
    prompt_id = seed.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) VALUES (1, ?, ?, 1)",
        (persona_text, model),
    ).lastrowid
    entry_row = dict(seed.execute("SELECT * FROM diary_entry WHERE id = ?", (entry_row_id,)).fetchone())
    envelope = llm_module.build_commentary_envelope(entry_row, [entry_row], persona_text, model)
    job_id, _ = enqueue_snapshot_regen_job(
        seed, prompt_version_id=prompt_id, preflight_entry_ids=[entry_row_id],
        targets=[PreparedRegenTarget(
            "entry_commentary", entry_row_id, envelope.assembly_version, envelope_fingerprint(envelope)
        )],
        owner_token="crashed-process",
    )
    item_id = seed.execute(
        "SELECT id FROM regen_job_item WHERE job_id = ? AND target_type = 'entry_commentary'",
        (job_id,),
    ).fetchone()["id"]
    seed.execute("UPDATE regen_job_item SET status = 'running' WHERE id = ?", (item_id,))
    seed.close()

    started = asyncio.Event()
    may_finish = asyncio.Event()

    async def _blocking_stream(envelope):
        started.set()
        await may_finish.wait()
        yield "should never be reached"

    monkeypatch.setattr(llm_module, "stream_completion_envelope", _blocking_stream)

    async def _fake_limit(model):
        return 200_000
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _fake_limit)

    shutdown_calls = []

    async def _fake_shutdown():
        shutdown_calls.append(True)
    async def _noop(): pass
    monkeypatch.setattr(llm_module, "warm_up_client", _noop)
    monkeypatch.setattr(llm_module, "shutdown_client", _fake_shutdown)
    monkeypatch.setenv("UNFLINCHER_DB", db_path)
    monkeypatch.setenv("UNFLINCHER_REQUIRE_ACCESS_AUTH", "false")

    app = create_app()
    async with app.router.lifespan_context(app):
        await started.wait()  # the recovered worker is now actively "streaming"
        recovery_task = app.state.recovery_task
        assert not recovery_task.done()
        # Exit the lifespan context WITHOUT ever unblocking the stream -- shutdown must still
        # complete cleanly rather than hang or tear down the client/connection out from under it.

    # The recovery task was cancelled and settled before shutdown_client() ran.
    assert recovery_task.done()
    assert shutdown_calls == [True]
    # The job was left exactly as an interrupted-not-crashed run leaves it -- never force-
    # cancelled -- so a future startup's recovery can resume it again.
    reopened = get_connection(db_path)
    job = reopened.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "running"
    from unflincher.db import entry_target_key, get_lease_by_target
    assert get_lease_by_target(reopened, entry_target_key(entry_row_id)) is None
    reopened.close()


async def test_startup_cancels_legacy_running_job_without_snapshot(tmp_path, monkeypatch):
    db_path = str(tmp_path / "legacy-recovery.db")

    # Seed a job the OLD way (no snapshot_entry_count -- e.g. written by code that predates this
    # workstream, or restored from a v0.1 backup): startup must NEVER resume it against the live
    # archive. It must be cancelled and its unfinished items deleted instead.
    seed = get_connection(db_path)
    init_schema(seed)
    migrate_persona_prompt_model(seed)
    migrate_generation_safety(seed)
    entry_id = seed.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('日记0', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    prompt_id = seed.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) VALUES (1, '人设', 'gpt-5.4', 1)"
    ).lastrowid
    job_id = seed.execute(
        "INSERT INTO regen_job (prompt_version_id, status) VALUES (?, 'running')", (prompt_id,)
    ).lastrowid  # no snapshot_entry_count -> legacy
    seed.execute(
        "INSERT INTO regen_job_item (job_id, target_type, entry_id, status) "
        "VALUES (?, 'entry_commentary', ?, 'running')",
        (job_id, entry_id),
    )
    seed.close()

    async def _noop(): pass
    monkeypatch.setattr(llm_module, "warm_up_client", _noop)
    monkeypatch.setattr(llm_module, "shutdown_client", _noop)
    monkeypatch.setenv("UNFLINCHER_DB", db_path)
    monkeypatch.setenv("UNFLINCHER_REQUIRE_ACCESS_AUTH", "false")

    app = create_app()
    async with app.router.lifespan_context(app):
        db = app.state.db
        job = db.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
        remaining_items = db.execute(
            "SELECT COUNT(*) AS n FROM regen_job_item WHERE job_id = ?", (job_id,)
        ).fetchone()["n"]
        commentary_count = db.execute(
            "SELECT COUNT(*) AS n FROM entry_commentary WHERE entry_id = ?", (entry_id,)
        ).fetchone()["n"]

    assert job["status"] == "cancelled"
    assert remaining_items == 0  # the unfinished item was deleted, never requeued
    assert commentary_count == 0  # never generated against the live archive
    assert not hasattr(app.state, "recovery_task")  # no worker was ever launched for it


def test_startup_runs_chat_session_migration(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    db_path = str(tmp_path / "migrate.db")
    monkeypatch.setenv("UNFLINCHER_DB", db_path)
    monkeypatch.setenv("UNFLINCHER_REQUIRE_ACCESS_AUTH", "false")

    async def _noop(): pass
    monkeypatch.setattr(llm_module, "warm_up_client", _noop)
    monkeypatch.setattr(llm_module, "shutdown_client", _noop)

    app = create_app()
    with TestClient(app) as c:
        c.get("/healthz")
        db = app.state.db
        columns = {r["name"] for r in db.execute("PRAGMA table_info(chat_message)")}
        assert "session_id" in columns


def test_startup_runs_generation_safety_migration(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    db_path = str(tmp_path / "migrate-gensafety.db")
    monkeypatch.setenv("UNFLINCHER_DB", db_path)
    monkeypatch.setenv("UNFLINCHER_REQUIRE_ACCESS_AUTH", "false")

    async def _noop(): pass
    monkeypatch.setattr(llm_module, "warm_up_client", _noop)
    monkeypatch.setattr(llm_module, "shutdown_client", _noop)

    app = create_app()
    with TestClient(app) as c:
        c.get("/healthz")
        db = app.state.db
        job_columns = {r["name"] for r in db.execute("PRAGMA table_info(regen_job)")}
        assert "snapshot_entry_count" in job_columns
        tables = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"maintenance_control", "generation_lease", "regen_job_entry_snapshot"} <= tables
        assert hasattr(app.state, "owner_token")
        assert isinstance(app.state.owner_token, str) and app.state.owner_token


def test_startup_and_shutdown_call_client_warm_up_and_shutdown(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import unflincher.llm as llm_module

    calls = []

    async def _fake_warm_up():
        calls.append("warm_up")

    async def _fake_shutdown():
        calls.append("shutdown")

    monkeypatch.setattr(llm_module, "warm_up_client", _fake_warm_up)
    monkeypatch.setattr(llm_module, "shutdown_client", _fake_shutdown)

    db_path = str(tmp_path / "lifespan-client.db")
    monkeypatch.setenv("UNFLINCHER_DB", db_path)
    monkeypatch.setenv("UNFLINCHER_REQUIRE_ACCESS_AUTH", "false")

    app = create_app()
    with TestClient(app) as c:
        c.get("/healthz")
        assert calls == ["warm_up"]

    assert calls == ["warm_up", "shutdown"]
