import diary.llm as llm_module


async def _fake_tokens(*args, **kwargs):
    for t in ["观察：", "你在", "逃避"]:
        yield t


async def _fake_multiline_tokens(*args, **kwargs):
    # One token carries paragraph breaks; sse-starlette serializes each embedded newline as a
    # separate `data: ` line inside a single event frame (the exact wire shape the browser SSE
    # parser must rejoin). Regression guard for the multi-line streamed-text corruption bug.
    yield "第一行\n第二行"
    yield "\n\n列表：\n- 项目一"


def test_generate_commentary_streams_and_persists(client, monkeypatch):
    monkeypatch.setattr(llm_module, "generate_commentary", _fake_tokens)
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('标题', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid

    response = client.post(f"/entry/{entry_id}/commentary")

    assert response.status_code == 200
    assert "观察：" in response.text
    assert "event: done" in response.text

    row = db.execute(
        "SELECT body_text, status FROM entry_commentary WHERE entry_id = ?", (entry_id,)
    ).fetchone()
    assert row["status"] == "ok"
    assert row["body_text"] == "观察：你在逃避"


def test_generate_commentary_404_for_missing_entry(client, monkeypatch):
    monkeypatch.setattr(llm_module, "generate_commentary", _fake_tokens)
    response = client.post("/entry/9999/commentary")
    assert response.status_code == 404


def test_commentary_multiline_token_rejoins_without_data_prefix_leak(client, monkeypatch):
    monkeypatch.setattr(llm_module, "generate_commentary", _fake_multiline_tokens)
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('标题', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid

    response = client.post(f"/entry/{entry_id}/commentary")
    assert response.status_code == 200

    # Server frame shape: a multi-line token IS split into several `data: ` lines inside one
    # `event: token` frame — exactly what the old single greedy-regex parser mis-handled.
    token_frames = [f for f in response.text.split("\n\n") if "event: token" in f]
    assert any(f.count("data: ") > 1 for f in token_frames)

    # Rejoin each frame the way app.js's parseSseFrame does; the reconstructed text must equal the
    # original tokens with NO stray "data: " field prefix embedded anywhere.
    rendered = "".join(
        "\n".join(line[6:] for line in f.split("\n") if line.startswith("data: "))
        for f in token_frames
    )
    assert rendered == "第一行\n第二行\n\n列表：\n- 项目一"
    assert "data: " not in rendered

    # The persisted row (the permanent source of truth once the stream ends) carries the clean
    # joined text, never a "data: " fragment.
    row = db.execute(
        "SELECT body_text, status FROM entry_commentary WHERE entry_id = ?", (entry_id,)
    ).fetchone()
    assert row["status"] == "ok"
    assert row["body_text"] == "第一行\n第二行\n\n列表：\n- 项目一"
    assert "data: " not in row["body_text"]


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

    assert "还没有锐评" in response.text


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
