import diary.llm as llm_module


async def _fake_tokens(*args, **kwargs):
    for t in ["观察：", "你在", "逃避"]:
        yield t


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
        "INSERT INTO persona_prompt (version_no, body_text, is_active) VALUES (2, 'p', 0)"
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
        "INSERT INTO persona_prompt (version_no, body_text, is_active) VALUES (2, 'p', 0)"
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
