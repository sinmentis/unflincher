import unflincher.llm as llm_module


def test_trigger_commentary_creates_background_job(client, monkeypatch):
    async def _fake_run_job(self, job_id, persona_text, model):
        pass  # don't actually run the worker in this test -- only the job creation is under test

    monkeypatch.setattr("unflincher.worker.BatchWorker.run_job", _fake_run_job)
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('标题', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid

    response = client.post(f"/entry/{entry_id}/commentary")

    assert response.status_code == 200
    assert "job_id" in response.json()
    items = db.execute(
        "SELECT * FROM regen_job_item WHERE target_type = 'entry_commentary' AND entry_id = ?",
        (entry_id,),
    ).fetchall()
    assert len(items) == 1


def test_trigger_commentary_404_for_missing_entry(client):
    response = client.post("/entry/9999/commentary")
    assert response.status_code == 404


def test_trigger_commentary_409_when_a_job_is_already_running(client):
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('a', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid

    # Directly create a running job to simulate "one already in flight" -- matches the
    # established pattern in test_routes_workshop.py's test_apply_all_rejects_concurrent_job:
    # inserting the job row alone is enough to hold the single-flight lock; no need to race a
    # real background task through the TestClient (which blocks on BackgroundTasks completion).
    from unflincher.db import get_active_prompt, start_single_entry_commentary_job
    active_prompt = get_active_prompt(db)
    start_single_entry_commentary_job(db, active_prompt["id"], entry_id)

    response = client.post(f"/entry/{entry_id}/commentary")
    assert response.status_code == 409


def test_entry_detail_shows_content(client):
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('标题', '<p>正文</p>', '<p>正文</p>', '正文', "
        "'2026-01-01', 'import')"
    ).lastrowid

    response = client.get(f"/entry/{entry_id}")

    assert response.status_code == 200
    assert "标题" in response.text
    assert "<p>正文</p>" in response.text


def test_entry_detail_shows_current_commentary_when_present(client):
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('标题', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) VALUES (2, 'p', 'test-model', 0)"
    ).lastrowid
    db.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status) "
        "VALUES (?, ?, 'm', '**这是锐评**', 'ok')",
        (entry_id, prompt_id),
    )

    response = client.get(f"/entry/{entry_id}")

    assert "<strong>这是锐评</strong>" in response.text


def test_entry_detail_shows_pending_state_when_no_commentary(client):
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('标题', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid

    response = client.get(f"/entry/{entry_id}")

    assert "No commentary yet." in response.text


def test_entry_detail_404_for_missing_entry(client):
    response = client.get("/entry/9999")
    assert response.status_code == 404


async def _fake_chat_tokens(*args, **kwargs):
    for t in ["那", "就", "先", "走", "一步"]:
        yield t


def test_entry_chat_persists_user_and_assistant_messages(client, monkeypatch):
    monkeypatch.setattr(llm_module, "chat_reply", _fake_chat_tokens)
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid

    response = client.post(f"/entry/{entry_id}/chat", json={"message": "怎么办"})

    assert response.status_code == 200
    # SSE frames separate every token, so the concatenation only exists in the DB row;
    # here we just prove the stream carried tokens through to completion.
    assert "一步" in response.text
    assert "event: done" in response.text

    rows = db.execute(
        "SELECT role, content FROM chat_message WHERE thread_kind='entry' AND entry_id=? ORDER BY id",
        (entry_id,),
    ).fetchall()
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[0]["content"] == "怎么办"
    assert rows[1]["content"] == "那就先走一步"


def test_entry_detail_renders_assistant_chat_markdown_but_not_user_text(client):
    # Same regression as the general chat page (tests/test_routes_chat.py): assistant replies
    # are markdown, user turns are plain typed text and must not be run through the markdown
    # pipeline.
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    db.execute(
        "INSERT INTO chat_message (thread_kind, entry_id, role, content) VALUES ('entry', ?, 'user', ?)",
        (entry_id, "1*2*3 是我写的"),
    )
    db.execute(
        "INSERT INTO chat_message (thread_kind, entry_id, role, content) VALUES ('entry', ?, 'assistant', ?)",
        (entry_id, "**这是重点**"),
    )

    response = client.get(f"/entry/{entry_id}")

    assert response.status_code == 200
    assert "<strong>这是重点</strong>" in response.text
    assert "**这是重点**" not in response.text
    assert "1*2*3" in response.text


def test_entry_chat_uses_latest_ok_commentary_not_a_specific_version(client, monkeypatch):
    captured = {}
    async def fake_chat_reply(entry_context, commentary_text, history, user_message, persona_text, model):
        captured["commentary_text"] = commentary_text
        yield "ok"

    monkeypatch.setattr(llm_module, "chat_reply", fake_chat_reply)
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    # is_active=0: the startup lifespan already seeds an active default persona, and the
    # partial unique index allows only one is_active=1 row. This prompt only needs to be a
    # valid FK target for the commentary rows below.
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) VALUES (2, 'p', 'test-model', 0)"
    ).lastrowid
    db.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status, created_at) "
        "VALUES (?, ?, 'm', '旧版本', 'ok', '2026-01-01T00:00:00')", (entry_id, prompt_id),
    )
    db.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status, created_at) "
        "VALUES (?, ?, 'm', '最新版本', 'ok', '2026-01-02T00:00:00')", (entry_id, prompt_id),
    )

    client.post(f"/entry/{entry_id}/chat", json={"message": "hi"})

    assert captured["commentary_text"] == "最新版本"


def test_view_specific_historical_commentary_version(client):
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    # is_active=0: the startup lifespan already seeds an active default persona and the
    # partial unique index allows only one is_active=1 row; this prompt is just a FK target.
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) VALUES (2, 'p', 'test-model', 0)"
    ).lastrowid
    old_id = db.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status, created_at) "
        "VALUES (?, ?, 'm', '旧版本内容', 'ok', '2026-01-01T00:00:00')", (entry_id, prompt_id),
    ).lastrowid
    db.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status, created_at) "
        "VALUES (?, ?, 'm', '最新版本内容', 'ok', '2026-01-02T00:00:00')", (entry_id, prompt_id),
    )

    response = client.get(f"/entry/{entry_id}/commentary/{old_id}")

    assert response.status_code == 200
    assert "旧版本内容" in response.text
    # browsing an old version must not affect the chat thread's own independent context —
    # this route only swaps the commentary display, per Global Constraints.


def test_entry_detail_uses_balanced_graphite_reading_layout(client):
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('A hard choice', '<p>Body</p>', '<p>Body</p>', "
        "'Body', '2026-07-13', 'manual')"
    ).lastrowid

    body = client.get(f"/entry/{entry_id}").text

    assert 'data-page="entry"' in body
    assert 'class="entry-layout"' in body
    assert 'data-role="primary-task"' in body
    assert 'id="diary-text"' in body
    assert 'data-role="entry-body"' in body
    assert 'data-role="ai-commentary"' in body
    assert 'id="ai-commentary"' not in body
    assert 'id="chat-section"' in body
    assert 'data-role="follow-up"' in body
    assert body.index('id="diary-text"') < body.index('id="chat-section"')
    assert 'data-entry-source="manual"' in body
    assert 'src="/static/js/entry.js"' in body


def test_entry_detail_preserves_generation_and_version_hooks(client):
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) "
        "VALUES (2, 'p', 'test-model', 0)"
    ).lastrowid
    ok_id = db.execute(
        "INSERT INTO entry_commentary "
        "(entry_id, prompt_version_id, model, body_text, status, created_at) "
        "VALUES (?, ?, 'test-model', 'current', 'ok', '2026-01-02T00:00:00')",
        (entry_id, prompt_id),
    ).lastrowid
    db.execute(
        "INSERT INTO entry_commentary "
        "(entry_id, prompt_version_id, model, body_text, status, error, created_at) "
        "VALUES (?, ?, 'test-model', '', 'failed', 'boom', '2026-01-01T00:00:00')",
        (entry_id, prompt_id),
    )
    body = client.get(f"/entry/{entry_id}").text
    assert 'id="ai-commentary"' in body
    assert 'data-role="ai-commentary"' in body
    assert f'href="/entry/{entry_id}/commentary/{ok_id}"' in body
    assert 'id="run-commentary"' in body or 'id="retry-commentary"' in body


def test_failed_historical_commentary_shows_local_failure_state(client):
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) "
        "VALUES (2, 'p', 'test-model', 0)"
    ).lastrowid
    failed_id = db.execute(
        "INSERT INTO entry_commentary "
        "(entry_id, prompt_version_id, model, body_text, status, error) "
        "VALUES (?, ?, 'test-model', '', 'failed', 'boom')",
        (entry_id, prompt_id),
    ).lastrowid

    body = client.get(f"/entry/{entry_id}/commentary/{failed_id}").text

    assert 'id="commentary-error"' in body
    assert "boom" in body


def test_entry_detail_has_toc_anchors_and_sidebar(client):
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid

    body = client.get(f"/entry/{entry_id}").text

    assert 'id="diary-text"' in body
    assert 'id="chat-section"' in body
    assert 'class="entry-margin-index"' in body
    assert 'href="#diary-text"' in body
    assert 'href="#chat-section"' in body
    # No commentary yet on this entry — its TOC link must not be offered.
    assert 'href="#ai-commentary"' not in body


def test_entry_detail_chat_uses_bubble_classes_per_role(client):
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    db.execute(
        "INSERT INTO chat_message (thread_kind, entry_id, role, content) VALUES ('entry', ?, 'user', ?)",
        (entry_id, "我是不是在逃避"),
    )
    db.execute(
        "INSERT INTO chat_message (thread_kind, entry_id, role, content) VALUES ('entry', ?, 'assistant', ?)",
        (entry_id, "看看你上个月写的那篇"),
    )

    body = client.get(f"/entry/{entry_id}").text

    assert 'class="conversation-message is-user"' in body
    assert 'class="conversation-message is-mentor"' in body


def test_entry_detail_shows_busy_state_when_job_is_running(client):
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) VALUES (2, 'p', 'm', 0)"
    ).lastrowid
    job_id = db.execute(
        "INSERT INTO regen_job (prompt_version_id, status) VALUES (?, 'running')", (prompt_id,)
    ).lastrowid
    db.execute(
        "INSERT INTO regen_job_item (job_id, target_type, entry_id, status) "
        "VALUES (?, 'entry_commentary', ?, 'running')", (job_id, entry_id),
    )

    response = client.get(f"/entry/{entry_id}")

    assert response.status_code == 200
    assert "Generating commentary…" in response.text
    assert 'id="run-commentary"' not in response.text


def test_entry_detail_shows_failure_state_and_retry_button(client):
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) VALUES (2, 'p', 'm', 0)"
    ).lastrowid
    job_id = db.execute(
        "INSERT INTO regen_job (prompt_version_id, status) VALUES (?, 'done')", (prompt_id,)
    ).lastrowid
    item_id = db.execute(
        "INSERT INTO regen_job_item (job_id, target_type, entry_id, status) "
        "VALUES (?, 'entry_commentary', ?, 'failed')", (job_id, entry_id),
    ).lastrowid
    from unflincher.db import fail_job_item
    fail_job_item(db, item_id, "模型报错了")

    response = client.get(f"/entry/{entry_id}")

    assert response.status_code == 200
    assert "模型报错了" in response.text
    assert 'id="retry-commentary"' in response.text


def test_commentary_status_route_returns_polling_fragment_while_busy(client):
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) VALUES (2, 'p', 'm', 0)"
    ).lastrowid
    job_id = db.execute(
        "INSERT INTO regen_job (prompt_version_id, status) VALUES (?, 'running')", (prompt_id,)
    ).lastrowid
    db.execute(
        "INSERT INTO regen_job_item (job_id, target_type, entry_id, status) "
        "VALUES (?, 'entry_commentary', ?, 'pending')", (job_id, entry_id),
    )

    response = client.get(f"/entry/{entry_id}/commentary-status")

    assert response.status_code == 200
    assert "hx-trigger" in response.text
    assert "location.reload" not in response.text


def test_commentary_status_route_returns_refresh_signal_once_done(client):
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) VALUES (2, 'p', 'm', 0)"
    ).lastrowid
    job_id = db.execute(
        "INSERT INTO regen_job (prompt_version_id, status) VALUES (?, 'done')", (prompt_id,)
    ).lastrowid
    db.execute(
        "INSERT INTO regen_job_item (job_id, target_type, entry_id, status) "
        "VALUES (?, 'entry_commentary', ?, 'ok')", (job_id, entry_id),
    )

    response = client.get(f"/entry/{entry_id}/commentary-status")

    assert response.status_code == 204
    assert response.headers["HX-Refresh"] == "true"
    assert response.text == ""
