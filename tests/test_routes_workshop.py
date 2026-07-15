import pytest

import unflincher.llm as llm_module
from unflincher.db import start_regen_job


@pytest.fixture(autouse=True)
def _fake_model_limit(monkeypatch):
    """Every workshop route that generates (preview, apply-all, retry) now preflights against
    get_model_max_prompt_tokens() before opening SSE or enqueueing -- fake it so tests never need
    a real Copilot client just to pass preflight."""
    async def _fake_limit(model):
        return 200_000
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _fake_limit)


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


def test_workshop_page_shows_active_prompt_and_entry_dropdown(client, monkeypatch):
    async def _fake_models():
        return [("test-model", "Test Model")]
    monkeypatch.setattr(llm_module, "list_available_models", _fake_models)

    db = client.app.state.db
    _seed_entries(db)

    response = client.get("/workshop")

    assert response.status_code == 200
    assert "Use the Analyst perspective." in response.text  # fresh-install Analyst seed
    assert "日记0" in response.text and "日记1" in response.text


def test_workshop_renders_draft_test_commit_stages(client):
    body = client.get("/workshop").text
    assert 'data-workshop-stage="draft"' in body
    assert 'data-workshop-stage="test"' in body
    assert 'data-workshop-stage="commit"' in body
    assert 'id="apply-all-confirmation"' in body
    assert 'id="workshop-notice"' in body
    assert 'src="/static/js/workshop.js"' in body
    assert "<script>" not in body


def test_workshop_renders_balanced_draft_test_commit_stages(client):
    body = client.get("/workshop").text
    assert 'class="workshop-layout"' in body
    assert 'data-role="primary-task"' in body
    assert 'data-workshop-stage="draft"' in body
    assert 'data-workshop-stage="test"' in body
    assert 'data-workshop-stage="commit"' in body
    assert 'data-role="test-preview"' in body
    assert 'id="apply-all-confirmation"' in body
    assert 'id="workshop-notice"' in body
    assert 'src="/static/js/workshop.js"' in body


def test_job_progress_uses_progress_rail(client):
    db = client.app.state.db
    prompt_id = db.execute("SELECT id FROM persona_prompt WHERE is_active = 1").fetchone()["id"]
    job_id = db.execute(
        "INSERT INTO regen_job (prompt_version_id, status) VALUES (?, 'done')",
        (prompt_id,),
    ).lastrowid
    db.execute(
        "INSERT INTO regen_job_item "
        "(job_id, target_type, entry_id, status, error) "
        "VALUES (?, 'aggregate_report', NULL, 'failed', 'boom')",
        (job_id,),
    )
    body = client.get(f"/workshop/jobs/{job_id}/progress").text
    assert 'class="progress-rail"' in body
    assert 'aria-live="polite"' in body
    assert 'id="regen-progress"' in body
    assert 'data-progress-bucket="10"' in body


def test_test_run_streams_but_writes_nothing_to_db(client, monkeypatch):
    monkeypatch.setattr(llm_module, "stream_completion_envelope", _fake_preview_tokens)
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
    monkeypatch.setattr(llm_module, "stream_completion_envelope", _fake_markdown_tokens)
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

    async def fake_stream(envelope):
        captured["user_content"] = envelope.user_content
        captured["system_content"] = envelope.system_content
        yield "x"

    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)
    db = client.app.state.db
    entry_ids = _seed_entries(db)

    client.post("/workshop/test-run", json={"draft_prompt": "草稿", "entry_id": entry_ids[0]})

    # Both seeded entries are in context, not just the 1 selected entry.
    assert "日记0" in captured["user_content"]
    assert "日记1" in captured["user_content"]
    assert captured["system_content"].startswith("草稿")


def test_apply_creates_new_active_prompt_version_without_generating(client, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("apply must not call the LLM")
    monkeypatch.setattr(llm_module, "stream_completion_envelope", _boom)
    db = client.app.state.db

    response = client.post("/workshop/apply", json={"draft_prompt": "新的正式人设", "model": "claude-sonnet-4.6"})

    assert response.status_code == 200
    active = db.execute("SELECT body_text FROM persona_prompt WHERE is_active = 1").fetchone()
    assert active["body_text"] == "新的正式人设"


def _dispatch_by_target(entry_commentary_gen, report_gen):
    """Build one fake stream_completion_envelope that routes to entry_commentary_gen(envelope) or
    report_gen(envelope) based on envelope.target_kind -- needed since apply-all/job-progress
    tests fake both target types through the SAME monkeypatched low-level function."""
    async def _fake(envelope):
        gen = entry_commentary_gen if envelope.target_kind == "entry_commentary" else report_gen
        async for tok in gen(envelope):
            yield tok
    return _fake


async def _fake_gen_ok(envelope):
    yield f"锐评-{envelope.target_id}"


async def _fake_report_ok(envelope):
    yield "报告"


def test_apply_all_processes_every_current_entry_not_a_fixed_count(client, monkeypatch):
    monkeypatch.setattr(
        llm_module, "stream_completion_envelope", _dispatch_by_target(_fake_gen_ok, _fake_report_ok)
    )
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
    from unflincher.db import get_active_prompt, start_regen_job
    active_prompt = get_active_prompt(db)
    start_regen_job(db, active_prompt["id"], [1])

    response = client.post("/workshop/apply-all")
    assert response.status_code == 409


def test_apply_and_regenerate_atomically_activates_prompt_used_by_job(client, monkeypatch):
    generation_inputs = []

    async def fake_stream(envelope):
        generation_inputs.append((envelope.system_content, envelope.model))
        yield f"take-{envelope.target_kind}"

    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)
    db = client.app.state.db
    _seed_entries(db)

    response = client.post(
        "/workshop/apply-all",
        json={"draft_prompt": "atomic prompt", "model": "test-model"},
    )

    assert response.status_code == 200
    active = db.execute(
        "SELECT id, body_text, model FROM persona_prompt WHERE is_active = 1"
    ).fetchone()
    job = db.execute(
        "SELECT prompt_version_id FROM regen_job WHERE id = ?",
        (response.json()["job_id"],),
    ).fetchone()
    assert active["body_text"] == "atomic prompt"
    assert active["model"] == "test-model"
    assert job["prompt_version_id"] == active["id"]
    assert all(system_content.startswith("atomic prompt") for system_content, _ in generation_inputs)
    assert all(model == "test-model" for _, model in generation_inputs)


def test_apply_and_regenerate_busy_rejection_does_not_save_prompt(client):
    db = client.app.state.db
    entry_ids = _seed_entries(db)
    original = db.execute(
        "SELECT id, body_text, model FROM persona_prompt WHERE is_active = 1"
    ).fetchone()
    before_count = db.execute("SELECT COUNT(*) AS n FROM persona_prompt").fetchone()["n"]
    start_regen_job(db, original["id"], entry_ids)

    response = client.post(
        "/workshop/apply-all",
        json={"draft_prompt": "must roll back", "model": "other-model"},
    )

    assert response.status_code == 409
    active = db.execute(
        "SELECT id, body_text, model FROM persona_prompt WHERE is_active = 1"
    ).fetchone()
    after_count = db.execute("SELECT COUNT(*) AS n FROM persona_prompt").fetchone()["n"]
    assert dict(active) == dict(original)
    assert after_count == before_count


def test_job_progress_reports_counts_and_failed_items(client, monkeypatch):
    db = client.app.state.db
    entry_ids = _seed_entries(db)
    failing_entry_id = entry_ids[1]  # "日记1"

    async def fake_gen(envelope):
        if envelope.target_id == str(failing_entry_id):
            raise RuntimeError("boom")
        yield "ok"

    monkeypatch.setattr(
        llm_module, "stream_completion_envelope", _dispatch_by_target(fake_gen, _fake_report_ok)
    )

    job_id = client.post("/workshop/apply-all").json()["job_id"]
    body = client.get(f"/workshop/jobs/{job_id}/progress").text

    assert "1 failed" in body
    assert "Retry" in body


def test_retry_failed_item_reopens_job_and_succeeds(client, monkeypatch):
    # Track attempts per entry id so "日记1 fails the FIRST time it is generated, succeeds on
    # retry" holds regardless of the order the worker happens to process items in. (A shared
    # global call counter would be order-dependent: the worker deterministically processes 日记0
    # before 日记1, so 日记1's first attempt is call #2, and a `<= 1` guard would never fire.)
    db = client.app.state.db
    entry_ids = _seed_entries(db)
    failing_entry_id = entry_ids[1]  # "日记1"
    attempts = {}

    async def fake_gen(envelope):
        attempts[envelope.target_id] = attempts.get(envelope.target_id, 0) + 1
        if envelope.target_id == str(failing_entry_id) and attempts[envelope.target_id] == 1:
            raise RuntimeError("boom")
        yield "recovered"

    monkeypatch.setattr(
        llm_module, "stream_completion_envelope", _dispatch_by_target(fake_gen, _fake_report_ok)
    )

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
    db = client.app.state.db
    entry_ids = _seed_entries(db)
    failing_entry_id = entry_ids[1]  # "日记1"
    attempts = {}

    async def fake_gen(envelope):
        attempts[envelope.target_id] = attempts.get(envelope.target_id, 0) + 1
        if envelope.target_id == str(failing_entry_id) and attempts[envelope.target_id] == 1:
            raise RuntimeError("boom")
        yield "recovered"

    monkeypatch.setattr(
        llm_module, "stream_completion_envelope", _dispatch_by_target(fake_gen, _fake_report_ok)
    )

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
    assert "queued" in response.text
    assert '"status"' not in response.text


def test_workshop_page_shows_model_select_with_active_model_selected(client, monkeypatch):
    async def _fake_models():
        return [("gpt-5.5", "GPT-5.5"), ("claude-opus-4.8", "Claude Opus 4.8")]
    monkeypatch.setattr(llm_module, "list_available_models", _fake_models)

    db = client.app.state.db
    _seed_entries(db)
    from unflincher.db import set_active_prompt
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
    assert 'id="model-notice"' in response.text
    assert 'data-tone="failed"' in response.text
    assert "Copilot client unavailable" in response.text
    # #model-select must exist in DOM even on error so JS handlers never get null
    assert 'id="model-select"' in response.text


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
    monkeypatch.setattr(llm_module, "stream_completion_envelope", _boom)
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

    async def fake_gen(envelope):
        captured["model"] = envelope.model
        yield "x"

    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_gen)
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

    async def fake_gen(envelope):
        captured["model"] = envelope.model
        yield "x"

    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_gen)
    db = client.app.state.db
    entry_ids = _seed_entries(db)
    from unflincher.db import set_active_prompt
    set_active_prompt(db, "人设", "claude-opus-4.8")

    client.post("/workshop/test-run", json={"draft_prompt": "草稿", "entry_id": entry_ids[0]})

    # No model in the body → fall back to the active persona's saved model.
    assert captured["model"] == "claude-opus-4.8"


def test_apply_all_uses_active_persona_model(client, monkeypatch):
    monkeypatch.setattr(
        llm_module, "stream_completion_envelope", _dispatch_by_target(_fake_gen_ok, _fake_report_ok)
    )
    db = client.app.state.db
    entry_ids = _seed_entries(db)
    from unflincher.db import set_active_prompt
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
    db = client.app.state.db
    entry_ids = _seed_entries(db)
    failing_entry_id = entry_ids[1]  # "日记1"
    attempts = {}

    async def fake_gen(envelope):
        attempts[envelope.target_id] = attempts.get(envelope.target_id, 0) + 1
        if envelope.target_id == str(failing_entry_id) and attempts[envelope.target_id] == 1:
            raise RuntimeError("boom")
        yield "recovered"

    monkeypatch.setattr(
        llm_module, "stream_completion_envelope", _dispatch_by_target(fake_gen, _fake_report_ok)
    )

    from unflincher.db import set_active_prompt
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


def test_test_run_404_for_missing_entry_before_opening_sse(client):
    response = client.post(
        "/workshop/test-run", json={"draft_prompt": "草稿", "entry_id": 999999},
    )
    assert response.status_code == 404


def test_test_run_413_when_context_too_large(client, monkeypatch):
    async def _tiny_limit(model):
        return 1
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _tiny_limit)

    db = client.app.state.db
    entry_ids = _seed_entries(db)

    response = client.post(
        "/workshop/test-run", json={"draft_prompt": "草稿" * 500, "entry_id": entry_ids[0]},
    )

    assert response.status_code == 413
    assert response.json()["detail"]["reason"] == "context_too_large"


def test_apply_all_413_writes_nothing_and_activates_no_prompt(client, monkeypatch):
    async def _tiny_limit(model):
        return 1
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _tiny_limit)

    db = client.app.state.db
    _seed_entries(db)
    before_count = db.execute("SELECT COUNT(*) AS n FROM persona_prompt").fetchone()["n"]

    response = client.post(
        "/workshop/apply-all", json={"draft_prompt": "草稿" * 500, "model": "test-model"},
    )

    assert response.status_code == 413
    assert response.json()["detail"]["reason"] == "context_too_large"
    assert db.execute("SELECT COUNT(*) AS n FROM regen_job").fetchone()["n"] == 0
    assert db.execute("SELECT COUNT(*) AS n FROM persona_prompt").fetchone()["n"] == before_count


def test_apply_all_releases_every_target_lease_after_success(client, monkeypatch):
    from unflincher.db import entry_target_key, get_lease_by_target, report_target_key

    monkeypatch.setattr(
        llm_module, "stream_completion_envelope", _dispatch_by_target(_fake_gen_ok, _fake_report_ok)
    )
    db = client.app.state.db
    entry_ids = _seed_entries(db)

    client.post("/workshop/apply-all")

    for entry_id in entry_ids:
        assert get_lease_by_target(db, entry_target_key(entry_id)) is None
    assert get_lease_by_target(db, report_target_key()) is None


def test_apply_all_409_when_an_entry_target_already_leased(client):
    from unflincher.db import acquire_lease, entry_target_key

    db = client.app.state.db
    entry_ids = _seed_entries(db)
    acquire_lease(db, entry_target_key(entry_ids[0]), "direct", "someone-else")

    response = client.post("/workshop/apply-all")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "target_busy"
    assert db.execute("SELECT COUNT(*) AS n FROM regen_job").fetchone()["n"] == 0


def test_retry_job_item_409_when_maintenance_locked(client, monkeypatch):
    from unflincher.db import set_maintenance_locked

    db = client.app.state.db
    _seed_entries(db)
    monkeypatch.setattr(
        llm_module, "stream_completion_envelope", _dispatch_by_target(_fake_gen_ok, _fake_report_ok)
    )
    job_id = client.post("/workshop/apply-all").json()["job_id"]

    # Manufacture a failed item directly (this job actually succeeded) to exercise retry's
    # maintenance check in isolation.
    item = db.execute(
        "SELECT id FROM regen_job_item WHERE job_id = ? LIMIT 1", (job_id,)
    ).fetchone()
    db.execute("UPDATE regen_job_item SET status = 'failed' WHERE id = ?", (item["id"],))
    set_maintenance_locked(db, True)

    response = client.post(f"/workshop/jobs/{job_id}/item/{item['id']}/retry")

    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "maintenance_locked"


def test_test_run_acquires_and_releases_request_lease_on_success(client, monkeypatch):
    monkeypatch.setattr(llm_module, "stream_completion_envelope", _fake_preview_tokens)
    db = client.app.state.db
    entry_ids = _seed_entries(db)

    response = client.post(
        "/workshop/test-run", json={"draft_prompt": "草稿", "entry_id": entry_ids[0]},
    )

    assert response.status_code == 200
    # No lease remains once the preview stream has finished.
    assert db.execute("SELECT COUNT(*) AS n FROM generation_lease").fetchone()["n"] == 0


def test_test_run_503_when_maintenance_locked_writes_no_lease(client):
    from unflincher.db import set_maintenance_locked

    db = client.app.state.db
    entry_ids = _seed_entries(db)
    set_maintenance_locked(db, True)

    response = client.post(
        "/workshop/test-run", json={"draft_prompt": "草稿", "entry_id": entry_ids[0]},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "maintenance_locked"
    assert db.execute("SELECT COUNT(*) AS n FROM generation_lease").fetchone()["n"] == 0


def test_test_run_404_releases_request_lease(client):
    db = client.app.state.db
    _seed_entries(db)

    response = client.post(
        "/workshop/test-run", json={"draft_prompt": "草稿", "entry_id": 999999},
    )

    assert response.status_code == 404
    assert db.execute("SELECT COUNT(*) AS n FROM generation_lease").fetchone()["n"] == 0


def test_test_run_uses_canonical_archive_order_for_same_date_entries(client, monkeypatch):
    captured = {}

    async def fake_stream(envelope):
        captured["user_content"] = envelope.user_content
        yield "x"

    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)
    db = client.app.state.db
    # Two entries sharing the SAME entry_date, inserted in REVERSE of their intended (entry_date,
    # id) order -- id must be the deciding tiebreaker.
    later_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('B在后', '<p>b</p>', '<p>b</p>', 'b', '2026-03-01', 'import')"
    ).lastrowid
    earlier_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('A在前', '<p>a</p>', '<p>a</p>', 'a', '2026-03-01', 'import')"
    ).lastrowid
    assert earlier_id > later_id  # sanity: id order is the OPPOSITE of the desired output order

    client.post("/workshop/test-run", json={"draft_prompt": "草稿", "entry_id": earlier_id})

    assert captured["user_content"].index("B在后") < captured["user_content"].index("A在前")


def test_retry_job_item_rejects_when_job_not_done(client, monkeypatch):
    """A failed item cannot be retried while its owning job is still 'running' -- another worker
    may still be actively driving the rest of it; a second worker for the same job_id must never
    be scheduled."""
    db = client.app.state.db
    entry_ids = _seed_entries(db)
    e2 = entry_ids[1]

    async def fake_gen(envelope):
        if envelope.target_id == str(e2):
            raise RuntimeError("boom")
        yield "ok"

    monkeypatch.setattr(
        llm_module, "stream_completion_envelope", _dispatch_by_target(fake_gen, _fake_report_ok)
    )
    job_id = client.post("/workshop/apply-all").json()["job_id"]
    failed_item = db.execute(
        "SELECT id FROM regen_job_item WHERE job_id = ? AND status = 'failed'", (job_id,)
    ).fetchone()
    # Force the job back to 'running' as if another worker were still actively driving it.
    db.execute("UPDATE regen_job SET status = 'running' WHERE id = ?", (job_id,))

    response = client.post(f"/workshop/jobs/{job_id}/item/{failed_item['id']}/retry")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "stale_or_superseded"
    item = db.execute("SELECT status FROM regen_job_item WHERE id = ?", (failed_item["id"],)).fetchone()
    assert item["status"] == "failed"  # never requeued


def test_retry_job_item_404_when_item_does_not_belong_to_url_job_id(client, monkeypatch):
    db = client.app.state.db
    entry_ids = _seed_entries(db)
    e1 = entry_ids[0]

    async def fake_gen(envelope):
        if envelope.target_id == str(e1):
            raise RuntimeError("boom")
        yield "ok"

    monkeypatch.setattr(
        llm_module, "stream_completion_envelope", _dispatch_by_target(fake_gen, _fake_report_ok)
    )
    job_id = client.post("/workshop/apply-all").json()["job_id"]
    failed_item = db.execute(
        "SELECT id FROM regen_job_item WHERE job_id = ? AND status = 'failed'", (job_id,)
    ).fetchone()

    response = client.post(f"/workshop/jobs/{job_id + 999}/item/{failed_item['id']}/retry")

    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "item_job_mismatch"
    item = db.execute("SELECT status FROM regen_job_item WHERE id = ?", (failed_item["id"],)).fetchone()
    assert item["status"] == "failed"  # no-write; never requeued


def test_retry_job_item_409_request_format_changed_never_requeues(client, monkeypatch):
    """Acceptance 811-813: retry must REFUSE TO RUN (no requeue-then-fail-later) when the
    reconstructed request's fingerprint no longer matches what was stored at enqueue time."""
    db = client.app.state.db
    entry_ids = _seed_entries(db)
    e1 = entry_ids[0]

    async def fake_gen(envelope):
        if envelope.target_id == str(e1):
            raise RuntimeError("boom")
        yield "ok"

    monkeypatch.setattr(
        llm_module, "stream_completion_envelope", _dispatch_by_target(fake_gen, _fake_report_ok)
    )
    job_id = client.post("/workshop/apply-all").json()["job_id"]
    failed_item = db.execute(
        "SELECT id FROM regen_job_item WHERE job_id = ? AND status = 'failed'", (job_id,)
    ).fetchone()
    db.execute(
        "UPDATE regen_job_item SET request_fingerprint = 'stale-fingerprint' WHERE id = ?",
        (failed_item["id"],),
    )

    response = client.post(f"/workshop/jobs/{job_id}/item/{failed_item['id']}/retry")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "request_format_changed"
    item = db.execute("SELECT status FROM regen_job_item WHERE id = ?", (failed_item["id"],)).fetchone()
    assert item["status"] == "failed"  # never requeued -- proves no requeue-then-fail-later


def test_retry_job_item_413_when_current_context_too_large_never_requeues(client, monkeypatch):
    db = client.app.state.db
    entry_ids = _seed_entries(db)
    e1 = entry_ids[0]

    async def fake_gen(envelope):
        if envelope.target_id == str(e1):
            raise RuntimeError("boom")
        yield "ok"

    monkeypatch.setattr(
        llm_module, "stream_completion_envelope", _dispatch_by_target(fake_gen, _fake_report_ok)
    )
    job_id = client.post("/workshop/apply-all").json()["job_id"]
    failed_item = db.execute(
        "SELECT id FROM regen_job_item WHERE job_id = ? AND status = 'failed'", (job_id,)
    ).fetchone()

    async def _tiny_limit(model):
        return 1
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _tiny_limit)

    response = client.post(f"/workshop/jobs/{job_id}/item/{failed_item['id']}/retry")

    assert response.status_code == 413
    assert response.json()["detail"]["reason"] == "context_too_large"
    item = db.execute("SELECT status FROM regen_job_item WHERE id = ?", (failed_item["id"],)).fetchone()
    assert item["status"] == "failed"  # never requeued
