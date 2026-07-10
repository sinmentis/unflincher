"""Startup crash-recovery integration test.

Drives create_app()'s lifespan directly (no TestClient) so we can pre-seed a crashed job on
disk BEFORE the lifespan runs, then await app.state.recovery_task to force the relaunched
worker to finish deterministically — proving the resume path end-to-end at app startup.
"""
import diary.llm as llm_module
from diary.app import create_app
from diary.db import get_connection, init_schema, migrate_chat_session, migrate_persona_prompt_model


async def _fake_commentary(entry, all_entries, persona_text, model):
    yield "锐评：崩溃后恢复生成"


async def test_startup_recovers_crashed_running_job(tmp_path, monkeypatch):
    db_path = str(tmp_path / "recovery.db")

    # Seed the DB BEFORE create_app() runs its lifespan: a job left mid-crash (its
    # entry_commentary item stuck 'running') plus its already-finished aggregate_report item,
    # matching Task 15's crash-simulation pattern. Use a throwaway connection, then close it.
    seed = get_connection(db_path)
    init_schema(seed)
    migrate_persona_prompt_model(seed)
    entry_id = seed.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('日记0', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    # Seed a NON-default model (settings.llm_model defaults to claude-sonnet-4.6) so the assertion
    # below proves the recovered worker uses the job's OWN persona model, not the env default.
    prompt_id = seed.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) VALUES (1, '人设', 'gpt-5.4', 1)"
    ).lastrowid
    job_id = seed.execute(
        "INSERT INTO regen_job (prompt_version_id, status) VALUES (?, 'running')", (prompt_id,)
    ).lastrowid
    seed.execute(
        "INSERT INTO regen_job_item (job_id, target_type, entry_id, status) "
        "VALUES (?, 'entry_commentary', ?, 'running')",
        (job_id, entry_id),
    )
    seed.execute(
        "INSERT INTO regen_job_item (job_id, target_type, entry_id, status) "
        "VALUES (?, 'aggregate_report', NULL, 'ok')",
        (job_id,),
    )
    seed.close()

    # Module-reference monkeypatch (the worker calls llm.generate_commentary) so no real LLM hit.
    monkeypatch.setattr(llm_module, "generate_commentary", _fake_commentary)
    async def _noop(): pass
    monkeypatch.setattr(llm_module, "warm_up_client", _noop)
    monkeypatch.setattr(llm_module, "shutdown_client", _noop)
    monkeypatch.setenv("DIARY_DB", db_path)
    monkeypatch.setenv("DIARY_REQUIRE_ACCESS_AUTH", "false")

    app = create_app()
    async with app.router.lifespan_context(app):
        # The lifespan detected the 'running' job and relaunched the worker; await its task to
        # force deterministic completion before asserting on the resulting DB state.
        await app.state.recovery_task
        db = app.state.db
        job = db.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
        commentaries = db.execute(
            "SELECT status, model FROM entry_commentary WHERE entry_id = ?", (entry_id,)
        ).fetchall()

    assert job["status"] == "done"
    # Exactly one 'ok' row — the crash-safety property: resume never duplicates a result.
    assert len(commentaries) == 1
    assert commentaries[0]["status"] == "ok"
    # The recovered worker recorded the job's own persona model, not settings.llm_model.
    assert commentaries[0]["model"] == "gpt-5.4"


def test_startup_runs_chat_session_migration(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    db_path = str(tmp_path / "migrate.db")
    monkeypatch.setenv("DIARY_DB", db_path)
    monkeypatch.setenv("DIARY_REQUIRE_ACCESS_AUTH", "false")

    async def _noop(): pass
    monkeypatch.setattr(llm_module, "warm_up_client", _noop)
    monkeypatch.setattr(llm_module, "shutdown_client", _noop)

    app = create_app()
    with TestClient(app) as c:
        c.get("/healthz")
        db = app.state.db
        columns = {r["name"] for r in db.execute("PRAGMA table_info(chat_message)")}
        assert "session_id" in columns


def test_startup_and_shutdown_call_client_warm_up_and_shutdown(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import diary.llm as llm_module

    calls = []

    async def _fake_warm_up():
        calls.append("warm_up")

    async def _fake_shutdown():
        calls.append("shutdown")

    monkeypatch.setattr(llm_module, "warm_up_client", _fake_warm_up)
    monkeypatch.setattr(llm_module, "shutdown_client", _fake_shutdown)

    db_path = str(tmp_path / "lifespan-client.db")
    monkeypatch.setenv("DIARY_DB", db_path)
    monkeypatch.setenv("DIARY_REQUIRE_ACCESS_AUTH", "false")

    app = create_app()
    with TestClient(app) as c:
        c.get("/healthz")
        assert calls == ["warm_up"]

    assert calls == ["warm_up", "shutdown"]
