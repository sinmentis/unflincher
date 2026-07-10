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


async def _fake_markdown_tokens(*args, **kwargs):
    # Split across chunks the way a real streamed response would be -- the bold marker's closing
    # ** lands in a later token than its opening **, so this also proves the fix accumulates the
    # FULL text before rendering, rather than trying to markdown-render token-by-token.
    for t in ["**重点", "在这里**\n\n第二段。"]:
        yield t


def test_test_run_done_event_carries_rendered_markdown_html(client, monkeypatch):
    # Regression test: the workshop preview never reloads the page (unlike entry commentary/chat,
    # general chat, and the report page, which all call location.reload() and get a fresh
    # server-rendered pass) -- so if this route's `done` event doesn't hand back real HTML, the
    # preview is stuck showing raw markdown source (literal `**`) forever, and per the client-side
    # bug this fix also closes, it visually collapses once streaming ends because nothing ever
    # gives it real paragraph markup to replace the plain-text/pre-wrap rendering.
    monkeypatch.setattr(llm_module, "generate_commentary", _fake_markdown_tokens)
    db = client.app.state.db
    entry_ids = _seed_entries(db)

    response = client.post(
        "/workshop/test-run",
        json={"draft_prompt": "草稿", "entry_id": entry_ids[0]},
    )

    assert response.status_code == 200
    # The `done` event's JSON payload must contain sanitized HTML with the bold/paragraph markup
    # rendered -- not the literal `**` markdown source. (The raw `**重点` DOES legitimately appear
    # earlier in the SSE body -- that's the individual streamed `token` events, unrendered by
    # design while still in flight. Only the final `done` frame's payload is asserted here.)
    done_frame = response.text.split("event: done\n", 1)[1]
    assert "<strong>重点在这里</strong>" in done_frame
    assert "<p>第二段。</p>" in done_frame
    assert "**重点" not in done_frame


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

    response = client.post("/workshop/apply", json={"draft_prompt": "新的正式人设", "model": "claude-sonnet-4.6"})

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


def test_workshop_page_shows_model_select_with_active_model_selected(client, monkeypatch):
    async def _fake_models():
        return [("gpt-5.5", "GPT-5.5"), ("claude-opus-4.8", "Claude Opus 4.8")]
    monkeypatch.setattr(llm_module, "list_available_models", _fake_models)

    db = client.app.state.db
    _seed_entries(db)
    from diary.db import set_active_prompt
    set_active_prompt(db, "人设", "gpt-5.5")

    response = client.get("/workshop")

    assert response.status_code == 200
    assert 'id="model-select"' in response.text
    assert "GPT-5.5" in response.text
    assert "Claude Opus 4.8" in response.text
    assert 'value="gpt-5.5" selected' in response.text


def test_workshop_page_shows_error_when_model_list_fetch_fails(client, monkeypatch):
    async def _failing_models():
        raise RuntimeError("Copilot client unavailable")
    monkeypatch.setattr(llm_module, "list_available_models", _failing_models)

    response = client.get("/workshop")

    assert response.status_code == 200
    assert 'id="model-select"' not in response.text  # no dropdown when the fetch failed
    assert "stream-err" in response.text


def test_refresh_models_route_success(client, monkeypatch):
    async def _fake_refresh():
        return [("gpt-5.5", "GPT-5.5")]
    monkeypatch.setattr(llm_module, "refresh_available_models", _fake_refresh)

    response = client.post("/workshop/refresh-models")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_refresh_models_route_returns_409_when_busy(client, monkeypatch):
    async def _busy_refresh():
        raise RuntimeError("正在生成中，请稍后再刷新模型列表")
    monkeypatch.setattr(llm_module, "refresh_available_models", _busy_refresh)

    response = client.post("/workshop/refresh-models")

    assert response.status_code == 409
    assert "正在生成中" in response.json()["detail"]


def test_apply_saves_chosen_model(client, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("apply must not call the LLM")
    monkeypatch.setattr(llm_module, "generate_commentary", _boom)
    db = client.app.state.db

    response = client.post(
        "/workshop/apply", json={"draft_prompt": "带模型的人设", "model": "claude-opus-4.7"}
    )

    assert response.status_code == 200
    active = db.execute(
        "SELECT body_text, model FROM persona_prompt WHERE is_active = 1"
    ).fetchone()
    assert active["body_text"] == "带模型的人设"
    assert active["model"] == "claude-opus-4.7"


def test_test_run_uses_explicit_model_override(client, monkeypatch):
    captured = {}

    async def fake_gen(entry, all_entries, persona_text, model):
        captured["model"] = model
        yield "x"

    monkeypatch.setattr(llm_module, "generate_commentary", fake_gen)
    db = client.app.state.db
    entry_ids = _seed_entries(db)

    client.post(
        "/workshop/test-run",
        json={"draft_prompt": "草稿", "entry_id": entry_ids[0], "model": "gpt-5.3-codex"},
    )

    # An explicit model in the body wins, letting the owner trial a model without persisting it.
    assert captured["model"] == "gpt-5.3-codex"
    # ...and the override is never written to a persona version.
    persisted = db.execute(
        "SELECT COUNT(*) AS n FROM persona_prompt WHERE model = 'gpt-5.3-codex'"
    ).fetchone()["n"]
    assert persisted == 0


def test_test_run_falls_back_to_active_model_when_not_specified(client, monkeypatch):
    captured = {}

    async def fake_gen(entry, all_entries, persona_text, model):
        captured["model"] = model
        yield "x"

    monkeypatch.setattr(llm_module, "generate_commentary", fake_gen)
    db = client.app.state.db
    entry_ids = _seed_entries(db)
    from diary.db import set_active_prompt
    set_active_prompt(db, "人设", "claude-opus-4.8")

    client.post("/workshop/test-run", json={"draft_prompt": "草稿", "entry_id": entry_ids[0]})

    # No model in the body → fall back to the active persona's saved model.
    assert captured["model"] == "claude-opus-4.8"


def test_apply_all_uses_active_persona_model(client, monkeypatch):
    monkeypatch.setattr(llm_module, "generate_commentary", _fake_gen_ok)
    monkeypatch.setattr(llm_module, "generate_report", _fake_report_ok)
    db = client.app.state.db
    entry_ids = _seed_entries(db)
    from diary.db import set_active_prompt
    set_active_prompt(db, "人设X", "gpt-5.4-mini")

    client.post("/workshop/apply-all")

    row = db.execute(
        "SELECT model FROM entry_commentary WHERE entry_id = ?", (entry_ids[0],)
    ).fetchone()
    assert row["model"] == "gpt-5.4-mini"
    report = db.execute("SELECT model FROM aggregate_report WHERE status='ok'").fetchone()
    assert report["model"] == "gpt-5.4-mini"


def test_retry_uses_original_job_model_not_current_active(client, monkeypatch):
    # 日记1 fails on its first generation so there is a failed item to retry.
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

    from diary.db import set_active_prompt
    # The job runs under this persona/model...
    set_active_prompt(db, "人设A", "gpt-5.4")
    job_id = client.post("/workshop/apply-all").json()["job_id"]
    failed_item = db.execute(
        "SELECT id, entry_id FROM regen_job_item WHERE job_id=? AND status='failed'", (job_id,)
    ).fetchone()

    # ...then the active persona's model changes BEFORE the retry.
    set_active_prompt(db, "人设B", "gemini-3.1-pro-preview")

    client.post(f"/workshop/jobs/{job_id}/item/{failed_item['id']}/retry")

    # The retried item must record the ORIGINAL job's model, not today's active one.
    row = db.execute(
        "SELECT model FROM entry_commentary WHERE entry_id = ? ORDER BY created_at DESC LIMIT 1",
        (failed_item["entry_id"],),
    ).fetchone()
    assert row["model"] == "gpt-5.4"
