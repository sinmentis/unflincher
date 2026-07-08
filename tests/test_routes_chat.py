import diary.llm as llm_module


async def _fake_general_tokens(*args, **kwargs):
    for t in ["从", "全局", "看"]:
        yield t


def test_general_chat_page_shows_empty_state(client):
    response = client.get("/chat")
    assert response.status_code == 200


def test_general_chat_persists_and_replies(client, monkeypatch):
    monkeypatch.setattr(llm_module, "general_chat_reply", _fake_general_tokens)
    db = client.app.state.db
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    )

    response = client.post("/chat/message", json={"message": "我是不是一直在逃避"})

    assert response.status_code == 200
    # SSE frames separate every token onto its own `data:` line, so the full concatenation
    # never appears contiguously in response.text — only the last token / done event proves
    # the stream ran to completion. The concatenated reply lives in the DB row instead.
    assert "看" in response.text
    assert "event: done" in response.text

    rows = db.execute(
        "SELECT role, content, entry_id FROM chat_message WHERE thread_kind='general' ORDER BY id"
    ).fetchall()
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[0]["content"] == "我是不是一直在逃避"
    assert rows[1]["content"] == "从全局看"
    # General chat rows must never collide with per-entry chat rows: entry_id stays NULL.
    assert all(r["entry_id"] is None for r in rows)
