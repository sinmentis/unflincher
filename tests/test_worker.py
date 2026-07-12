import pytest

import unflincher.llm as llm_module
from unflincher.db import get_connection, get_current_commentary, init_schema, migrate_persona_prompt_model, resume_sweep, set_active_prompt, start_regen_job
from unflincher.worker import BatchWorker


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "test.db"))
    init_schema(c)
    migrate_persona_prompt_model(c)
    yield c
    c.close()


def _seed_entries(conn, n):
    return [
        conn.execute(
            "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
            "entry_date, source) VALUES (?, '<p>x</p>', '<p>x</p>', 'x', ?, 'import')",
            (f"日记{i}", f"2026-01-{i+1:02d}"),
        ).lastrowid
        for i in range(n)
    ]


async def test_run_job_generates_commentary_for_every_entry_and_the_report(monkeypatch, conn):
    async def fake_gen(entry, all_entries, persona_text, model):
        yield f"关于{entry['title']}的锐评"

    async def fake_report(all_entries, persona_text, model):
        yield "汇总报告"

    monkeypatch.setattr(llm_module, "generate_commentary", fake_gen)
    monkeypatch.setattr(llm_module, "generate_report", fake_report)

    entry_ids = _seed_entries(conn, 3)
    prompt_id = set_active_prompt(conn, "人设", "test-model")
    job_id = start_regen_job(conn, prompt_id, entry_ids)

    worker = BatchWorker(conn, concurrency=2)
    await worker.run_job(job_id, "人设", "test-model")

    for entry_id in entry_ids:
        current = get_current_commentary(conn, entry_id)
        assert current is not None
        assert "锐评" in current["body_text"]
    report = conn.execute("SELECT * FROM aggregate_report WHERE status='ok'").fetchone()
    assert report["body_text"] == "汇总报告"
    assert report["covered_entry_count"] == 3

    job = conn.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "done"


async def test_run_job_isolates_failures_and_continues(monkeypatch, conn):
    async def fake_gen(entry, all_entries, persona_text, model):
        # Fail the first entry (日记0 = lowest entry_id = items[0] below) so failure isolation
        # is asserted against the item the ORDER BY entry_id query returns first.
        if entry["title"] == "日记0":
            raise RuntimeError("provider error")
        yield "ok take"

    async def fake_report(all_entries, persona_text, model):
        yield "report"

    monkeypatch.setattr(llm_module, "generate_commentary", fake_gen)
    monkeypatch.setattr(llm_module, "generate_report", fake_report)

    entry_ids = _seed_entries(conn, 2)
    prompt_id = set_active_prompt(conn, "人设", "test-model")
    job_id = start_regen_job(conn, prompt_id, entry_ids)

    worker = BatchWorker(conn, concurrency=2)
    await worker.run_job(job_id, "人设", "test-model")

    items = conn.execute(
        "SELECT * FROM regen_job_item WHERE job_id = ? AND target_type='entry_commentary' ORDER BY entry_id",
        (job_id,),
    ).fetchall()
    assert items[0]["status"] == "failed"
    assert "provider error" in items[0]["error"]
    assert items[1]["status"] == "ok"
    # job still reaches 'done' — done means "finished", not "all succeeded"
    job = conn.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "done"


async def test_resume_sweep_requeues_running_items_without_duplicating_results(monkeypatch, conn):
    """Simulates a hard crash: an item stuck 'running' with no result yet (the process died
    before the LLM call returned). resume_sweep must requeue it; re-running the worker must
    produce exactly one commentary row, not two."""
    async def fake_gen(entry, all_entries, persona_text, model):
        yield "重新生成的锐评"

    monkeypatch.setattr(llm_module, "generate_commentary", fake_gen)
    monkeypatch.setattr(llm_module, "generate_report", lambda *a, **k: _empty_gen())

    entry_ids = _seed_entries(conn, 1)
    prompt_id = set_active_prompt(conn, "人设", "test-model")
    job_id = start_regen_job(conn, prompt_id, entry_ids)
    # Simulate the crash: mark the report item 'ok' (so we only re-drive the entry item) and
    # force the entry item into 'running' with no result_id, as if the worker claimed it but
    # died before calling complete_job_item.
    conn.execute(
        "UPDATE regen_job_item SET status='ok' WHERE job_id=? AND target_type='aggregate_report'",
        (job_id,),
    )
    conn.execute(
        "UPDATE regen_job_item SET status='running' WHERE job_id=? AND target_type='entry_commentary'",
        (job_id,),
    )

    reset_count = resume_sweep(conn)
    assert reset_count == 1

    worker = BatchWorker(conn, concurrency=1)
    await worker.run_job(job_id, "人设", "test-model")

    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM entry_commentary WHERE entry_id = ?", (entry_ids[0],)
    ).fetchone()
    assert rows["n"] == 1


async def _empty_gen():
    return
    yield  # pragma: no cover — makes this an async generator with zero items
