import pytest

import unflincher.llm as llm_module


@pytest.fixture(autouse=True)
def _fake_model_limit(monkeypatch):
    """Every route in this file now preflights against get_model_max_prompt_tokens() before
    generating or enqueueing -- fake it so tests never need a real Copilot client just to pass
    preflight."""
    async def _fake_limit(model):
        return 200_000
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _fake_limit)


def test_trigger_commentary_creates_background_job(client, monkeypatch):
    async def _fake_run_job(self, job_id):
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


def test_trigger_commentary_413_when_context_too_large(client, monkeypatch):
    async def _tiny_limit(model):
        return 1
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _tiny_limit)

    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('a', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid

    response = client.post(f"/entry/{entry_id}/commentary")

    assert response.status_code == 413
    assert response.json()["detail"]["reason"] == "context_too_large"
    # No write on this path.
    assert db.execute("SELECT COUNT(*) AS n FROM regen_job").fetchone()["n"] == 0


def test_trigger_commentary_503_when_model_limits_unavailable(client, monkeypatch):
    from unflincher.context_budget import ModelLimitsUnavailableError

    async def _raise(model):
        raise ModelLimitsUnavailableError(model, "boom")
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _raise)

    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('a', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid

    response = client.post(f"/entry/{entry_id}/commentary")

    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "model_limits_unavailable"


def test_trigger_commentary_409_when_entry_target_already_leased(client):
    from unflincher.db import entry_target_key, acquire_lease

    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('a', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    acquire_lease(db, entry_target_key(entry_id), "direct", "someone-else")

    response = client.post(f"/entry/{entry_id}/commentary")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "target_busy"


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

    assert "No reflection yet." in response.text


def test_entry_detail_404_for_missing_entry(client):
    response = client.get("/entry/9999")
    assert response.status_code == 404


async def _fake_chat_tokens(envelope):
    for t in ["那", "就", "先", "走", "一步"]:
        yield t


def test_entry_chat_persists_user_and_assistant_messages(client, monkeypatch):
    monkeypatch.setattr(llm_module, "stream_completion_envelope", _fake_chat_tokens)
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
    async def fake_stream(envelope):
        captured["system_content"] = envelope.system_content
        yield "ok"

    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)
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

    assert "最新版本" in captured["system_content"]
    assert "旧版本" not in captured["system_content"]


def test_entry_chat_releases_thread_lease_after_stream_completes(client, monkeypatch):
    from unflincher.db import entry_thread_key, get_lease_by_target

    async def fake_stream(envelope):
        yield "ok"
    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)

    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid

    client.post(f"/entry/{entry_id}/chat", json={"message": "hi"})

    assert get_lease_by_target(db, entry_thread_key(entry_id)) is None


def test_entry_chat_409_when_thread_already_busy(client):
    from unflincher.db import acquire_lease, entry_thread_key

    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    acquire_lease(db, entry_thread_key(entry_id), "thread", "someone-else")

    response = client.post(f"/entry/{entry_id}/chat", json={"message": "hi"})

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "target_busy"
    # No write on this no-write-409 path.
    assert db.execute("SELECT COUNT(*) AS n FROM chat_message").fetchone()["n"] == 0


def test_entry_chat_413_releases_lease_and_writes_no_message(client, monkeypatch):
    from unflincher.db import entry_thread_key, get_lease_by_target

    async def _tiny_limit(model):
        return 1
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _tiny_limit)

    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid

    response = client.post(f"/entry/{entry_id}/chat", json={"message": "hi"})

    assert response.status_code == 413
    assert response.json()["detail"]["reason"] == "context_too_large"
    assert db.execute("SELECT COUNT(*) AS n FROM chat_message").fetchone()["n"] == 0
    assert get_lease_by_target(db, entry_thread_key(entry_id)) is None


def test_commentary_version_route_no_longer_exists(client):
    """Entries only keep their latest reflection now -- there is nothing to browse, so the old
    per-version route is gone entirely rather than degraded."""
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) VALUES (2, 'p', 'test-model', 0)"
    ).lastrowid
    commentary_id = db.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status, created_at) "
        "VALUES (?, ?, 'm', '最新版本内容', 'ok', '2026-01-02T00:00:00')", (entry_id, prompt_id),
    ).lastrowid

    response = client.get(f"/entry/{entry_id}/commentary/{commentary_id}")

    assert response.status_code == 404


def test_entry_detail_only_ever_shows_the_latest_commentary_row(client):
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
    db.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status, created_at) "
        "VALUES (?, ?, 'm', '旧版本内容', 'ok', '2026-01-01T00:00:00')", (entry_id, prompt_id),
    )
    db.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status, created_at) "
        "VALUES (?, ?, 'm', '最新版本内容', 'ok', '2026-01-02T00:00:00')", (entry_id, prompt_id),
    )

    body = client.get(f"/entry/{entry_id}").text

    assert "最新版本内容" in body
    assert "旧版本内容" not in body
    assert 'class="version-ledger"' not in body


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
    assert 'class="entry-margin-index"' in body

    # Owner decision: keep chat last. The production render order is pinned to
    # diary-text -> commentary-section -> .entry-margin-index -> chat-section.
    # Assert on stable structural hooks only (ids/classes), never translated text.
    order_markers = [
        'id="diary-text"',
        'id="commentary-section"',
        'class="entry-margin-index"',
        'id="chat-section"',
    ]
    positions = [body.index(marker) for marker in order_markers]
    assert positions == sorted(positions), (
        "Entry Detail render order must be "
        "diary-text -> commentary-section -> .entry-margin-index -> chat-section; "
        f"got positions {dict(zip(order_markers, positions))}"
    )
    # Each hook must appear exactly once so the ordering above is unambiguous.
    for marker in order_markers:
        assert body.count(marker) == 1, f"expected exactly one {marker}"

    assert 'data-entry-source="manual"' in body
    assert 'src="/static/js/entry.js"' in body


def test_entry_detail_uses_quiet_record_metadata_without_ledger_numbering(client):
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('A hard choice', '<p>Body</p>', '<p>Body</p>', "
        "'Body', '2026-07-13', 'manual')"
    ).lastrowid

    body = client.get(f"/entry/{entry_id}").text

    assert 'class="record-metadata"' in body
    assert " / 001" not in body
    assert "Archive ID" not in body
    assert body.index('class="record-metadata"') < body.index('id="diary-text"')


def test_entry_detail_preserves_generation_hooks(client):
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) "
        "VALUES (2, 'p', 'test-model', 0)"
    ).lastrowid
    db.execute(
        "INSERT INTO entry_commentary "
        "(entry_id, prompt_version_id, model, body_text, status, created_at) "
        "VALUES (?, ?, 'test-model', 'current', 'ok', '2026-01-02T00:00:00')",
        (entry_id, prompt_id),
    )
    body = client.get(f"/entry/{entry_id}").text
    assert 'id="ai-commentary"' in body
    assert 'data-role="ai-commentary"' in body
    assert 'id="run-commentary"' in body or 'id="retry-commentary"' in body


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
    # No reflection yet on this entry, so its TOC link must not be offered.
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
    assert 'class="conversation-message is-assistant"' in body


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
    assert "Generating reflection…" in response.text
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


# ---------------------------------------------------------------------------
# Perspective indicator (Task: perspective indicators). Entry Reflection shows
# "Perspective: <name>" for whichever commentary version is displayed (current or historical),
# and the follow-up composer always shows "Perspective for the next response: <name>" for the
# globally active Perspective -- never the version being viewed.
# ---------------------------------------------------------------------------

def _insert_entry(db, title="t"):
    return db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES (?, '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')",
        (title,),
    ).lastrowid


def test_entry_detail_shows_perspective_for_current_commentary(client):
    db = client.app.state.db
    entry_id = _insert_entry(db)
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, preset_key, is_active) "
        "VALUES (2, 'p', 'test-model', 'companion', 0)"
    ).lastrowid
    db.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status) "
        "VALUES (?, ?, 'm', 'text', 'ok')",
        (entry_id, prompt_id),
    )

    body = client.get(f"/entry/{entry_id}").text

    assert "Perspective: Companion" in body


def test_entry_detail_shows_custom_for_null_preset_key(client):
    db = client.app.state.db
    entry_id = _insert_entry(db)
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) "
        "VALUES (2, 'p', 'test-model', 0)"
    ).lastrowid
    db.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status) "
        "VALUES (?, ?, 'm', 'text', 'ok')",
        (entry_id, prompt_id),
    )

    body = client.get(f"/entry/{entry_id}").text

    assert "Perspective: Custom" in body


def test_entry_detail_shows_custom_for_unknown_historical_preset_key(client):
    """A removed/historical preset key must render as Custom, never crash or leave a blank name."""
    db = client.app.state.db
    entry_id = _insert_entry(db)
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, preset_key, is_active) "
        "VALUES (2, 'p', 'test-model', 'retired-preset', 0)"
    ).lastrowid
    db.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status) "
        "VALUES (?, ?, 'm', 'text', 'ok')",
        (entry_id, prompt_id),
    )

    body = client.get(f"/entry/{entry_id}").text

    assert "Perspective: Custom" in body


def test_entry_detail_has_no_perspective_indicator_when_no_commentary_exists(client):
    db = client.app.state.db
    entry_id = _insert_entry(db)

    body = client.get(f"/entry/{entry_id}").text

    assert 'data-role="perspective-indicator"' not in body.split('id="chat-section"')[0]
    # The composer's "next response" indicator is independent and always shows the active
    # (fresh-install default) Perspective, even with no commentary yet.
    assert "Perspective for the next response: Analyst" in body


def test_entry_detail_composer_always_shows_the_globally_active_perspective(client):
    """The composer's indicator reflects the GLOBALLY active prompt, not whatever preset key is
    attached to the currently displayed commentary version."""
    db = client.app.state.db
    entry_id = _insert_entry(db)
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, preset_key, is_active) "
        "VALUES (2, 'p', 'test-model', 'challenger', 0)"
    ).lastrowid
    db.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status) "
        "VALUES (?, ?, 'm', 'text', 'ok')",
        (entry_id, prompt_id),
    )

    body = client.get(f"/entry/{entry_id}").text

    assert "Perspective: Challenger" in body
    # Fresh install seeds Analyst active -- the composer must show that, not Challenger.
    assert "Perspective for the next response: Analyst" in body


def test_entry_detail_perspective_indicator_translates(client):
    db = client.app.state.db
    entry_id = _insert_entry(db)
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, preset_key, is_active) "
        "VALUES (2, 'p', 'test-model', 'companion', 0)"
    ).lastrowid
    db.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status) "
        "VALUES (?, ?, 'm', 'text', 'ok')",
        (entry_id, prompt_id),
    )
    client.cookies.set("unflincher_lang", "de")

    body = client.get(f"/entry/{entry_id}").text

    assert "Perspektive: Begleiter" in body
    assert "Perspektive für die nächste Antwort: Analyst" in body


# ---------------------------------------------------------------------------
# Generation action naming (Task 6): "Generate reflection" the first time, "Regenerate
# reflection" once an Entry Reflection already exists.
# ---------------------------------------------------------------------------

def test_run_commentary_button_says_generate_when_no_reflection_exists_yet(client):
    db = client.app.state.db
    entry_id = _insert_entry(db)

    body = client.get(f"/entry/{entry_id}").text

    assert ">Generate reflection<" in body
    assert ">Regenerate reflection<" not in body


def test_run_commentary_button_says_regenerate_once_a_reflection_exists(client):
    db = client.app.state.db
    entry_id = _insert_entry(db)
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) "
        "VALUES (2, 'p', 'test-model', 0)"
    ).lastrowid
    db.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status) "
        "VALUES (?, ?, 'm', 'text', 'ok')",
        (entry_id, prompt_id),
    )

    body = client.get(f"/entry/{entry_id}").text

    assert ">Regenerate reflection<" in body
    assert ">Generate reflection<" not in body
