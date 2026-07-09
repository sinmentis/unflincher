import diary.llm as llm_module


async def _fake_preview_tokens(*args, **kwargs):
    for t in ["预览：", "这版语气", "更温和"]:
        yield t


def _seed_entries(db):
    return [
        db.execute(
            "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
            "entry_date, source) VALUES (?, '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')",
            (f"日记{i}",),
        ).lastrowid
        for i in range(2)
    ]


def test_workshop_page_shows_active_prompt_and_entry_dropdown(client):
    db = client.app.state.db
    _seed_entries(db)

    response = client.get("/workshop")

    assert response.status_code == 200
    assert "人生导师" in response.text  # default persona prompt text
    assert "日记0" in response.text and "日记1" in response.text


def test_test_run_streams_but_writes_nothing_to_db(client, monkeypatch):
    monkeypatch.setattr(llm_module, "generate_commentary", _fake_preview_tokens)
    db = client.app.state.db
    entry_ids = _seed_entries(db)

    before_prompts = db.execute("SELECT COUNT(*) AS n FROM persona_prompt").fetchone()["n"]
    before_commentary = db.execute("SELECT COUNT(*) AS n FROM entry_commentary").fetchone()["n"]

    response = client.post(
        "/workshop/test-run",
        json={"draft_prompt": "一个完全不同的草稿人设", "entry_id": entry_ids[0]},
    )

    assert response.status_code == 200
    # SSE frames separate every token, so the full concatenation never appears contiguously in
    # the raw body (rubber-duck correction #4). Assert the last token + terminal event instead.
    assert "更温和" in response.text
    assert "event: done" in response.text

    after_prompts = db.execute("SELECT COUNT(*) AS n FROM persona_prompt").fetchone()["n"]
    after_commentary = db.execute("SELECT COUNT(*) AS n FROM entry_commentary").fetchone()["n"]
    assert after_prompts == before_prompts
    assert after_commentary == before_commentary


def test_test_run_uses_full_corpus_same_as_real_generation(client, monkeypatch):
    captured = {}

    async def fake_generate(entry, all_entries, persona_text, model):
        captured["all_entries_count"] = len(all_entries)
        captured["persona_text"] = persona_text
        yield "x"

    monkeypatch.setattr(llm_module, "generate_commentary", fake_generate)
    db = client.app.state.db
    entry_ids = _seed_entries(db)

    client.post("/workshop/test-run", json={"draft_prompt": "草稿", "entry_id": entry_ids[0]})

    assert captured["all_entries_count"] == 2  # not just the 1 selected entry
    assert captured["persona_text"] == "草稿"


def test_apply_creates_new_active_prompt_version_without_generating(client, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("apply must not call the LLM")
    monkeypatch.setattr(llm_module, "generate_commentary", _boom)
    db = client.app.state.db

    response = client.post("/workshop/apply", json={"draft_prompt": "新的正式人设"})

    assert response.status_code == 200
    active = db.execute("SELECT body_text FROM persona_prompt WHERE is_active = 1").fetchone()
    assert active["body_text"] == "新的正式人设"


async def _fake_gen_ok(entry, all_entries, persona_text, model):
    yield f"锐评-{entry['title']}"


async def _fake_report_ok(all_entries, persona_text, model):
    yield "报告"


def test_apply_all_processes_every_current_entry_not_a_fixed_count(client, monkeypatch):
    monkeypatch.setattr(llm_module, "generate_commentary", _fake_gen_ok)
    monkeypatch.setattr(llm_module, "generate_report", _fake_report_ok)
    db = client.app.state.db
    entry_ids = _seed_entries(db)  # 2 entries from the earlier fixture helper
    # add a THIRD entry after the fixture helper's 2, proving apply-to-all isn't hardcoded
    third_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('日记2', '<p>x</p>', '<p>x</p>', 'x', '2026-01-03', 'import')"
    ).lastrowid

    response = client.post("/workshop/apply-all")
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    for eid in [*entry_ids, third_id]:
        row = db.execute(
            "SELECT status FROM entry_commentary WHERE entry_id = ?", (eid,)
        ).fetchone()
        assert row["status"] == "ok"
    job = db.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "done"


def test_apply_all_rejects_concurrent_job(client):
    db = client.app.state.db
    _seed_entries(db)

    # Directly create a running job to simulate "one already in flight" — start_regen_job
    # alone is enough to hold the single-flight lock; no need to race a real background task.
    from diary.db import get_active_prompt, start_regen_job
    active_prompt = get_active_prompt(db)
    start_regen_job(db, active_prompt["id"], [1])

    response = client.post("/workshop/apply-all")
    assert response.status_code == 409


def test_job_progress_reports_counts_and_failed_items(client, monkeypatch):
    async def fake_gen(entry, all_entries, persona_text, model):
        if entry["title"] == "日记1":
            raise RuntimeError("boom")
        yield "ok"

    monkeypatch.setattr(llm_module, "generate_commentary", fake_gen)
    monkeypatch.setattr(llm_module, "generate_report", _fake_report_ok)
    db = client.app.state.db
    _seed_entries(db)

    job_id = client.post("/workshop/apply-all").json()["job_id"]
    body = client.get(f"/workshop/jobs/{job_id}/progress").text

    assert "1 失败" in body
    assert "重试" in body


def test_retry_failed_item_reopens_job_and_succeeds(client, monkeypatch):
    # Track attempts per entry title so "日记1 fails the FIRST time it is generated, succeeds on
    # retry" holds regardless of the order the worker happens to process items in. (A shared
    # global call counter would be order-dependent: the worker deterministically processes 日记0
    # before 日记1, so 日记1's first attempt is call #2, and a `<= 1` guard would never fire.)
    attempts = {}

    async def fake_gen(entry, all_entries, persona_text, model):
        title = entry["title"]
        attempts[title] = attempts.get(title, 0) + 1
        if title == "日记1" and attempts[title] == 1:
            raise RuntimeError("boom")
        yield "recovered"

    monkeypatch.setattr(llm_module, "generate_commentary", fake_gen)
    monkeypatch.setattr(llm_module, "generate_report", _fake_report_ok)
    db = client.app.state.db
    _seed_entries(db)

    job_id = client.post("/workshop/apply-all").json()["job_id"]
    failed_item = db.execute(
        "SELECT id, entry_id FROM regen_job_item WHERE job_id=? AND status='failed'", (job_id,)
    ).fetchone()

    response = client.post(f"/workshop/jobs/{job_id}/item/{failed_item['id']}/retry")

    assert response.status_code == 200
    row = db.execute(
        "SELECT status FROM entry_commentary WHERE entry_id = ? ORDER BY created_at DESC LIMIT 1",
        (failed_item["entry_id"],),
    ).fetchone()
    assert row["status"] == "ok"


def test_retry_returns_progress_fragment_not_json(client, monkeypatch):
    # A failed item must exist to retry, so 日记1 fails on its first generation.
    attempts = {}

    async def fake_gen(entry, all_entries, persona_text, model):
        title = entry["title"]
        attempts[title] = attempts.get(title, 0) + 1
        if title == "日记1" and attempts[title] == 1:
            raise RuntimeError("boom")
        yield "recovered"

    monkeypatch.setattr(llm_module, "generate_commentary", fake_gen)
    monkeypatch.setattr(llm_module, "generate_report", _fake_report_ok)
    db = client.app.state.db
    _seed_entries(db)

    job_id = client.post("/workshop/apply-all").json()["job_id"]
    failed_item = db.execute(
        "SELECT id FROM regen_job_item WHERE job_id=? AND status='failed'", (job_id,)
    ).fetchone()

    response = client.post(f"/workshop/jobs/{job_id}/item/{failed_item['id']}/retry")

    assert response.status_code == 200
    # Retry must return the SAME html progress fragment the GET progress route returns, NOT the
    # old `{"status": "retrying"}` JSON that replaced the whole panel and stopped polling.
    assert response.headers["content-type"].startswith("text/html")
    assert 'id="regen-progress"' in response.text
    # The job was reopened to 'running', so the fragment re-carries the 2s polling trigger and
    # htmx keeps polling instead of freezing on stale content.
    assert "hx-trigger" in response.text
    assert "every 2s" in response.text
    # Structural parity with the GET progress fragment (same counts line), never raw JSON.
    assert "排队中" in response.text
    assert '"status"' not in response.text
