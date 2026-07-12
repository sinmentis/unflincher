import json

import unflincher.llm as llm_module


async def _fake_general_tokens(*args, **kwargs):
    for t in ["从", "全局", "看"]:
        yield t


async def _fake_title(first_message, model):
    return "该不该辞职"


async def _failing_title(first_message, model):
    raise RuntimeError("title model unavailable")


def test_chat_list_empty_state(client):
    response = client.get("/chat")
    assert response.status_code == 200
    assert "No conversations yet" in response.text


def test_chat_list_shows_existing_sessions(client):
    db = client.app.state.db
    db.execute("INSERT INTO chat_session (title) VALUES ('2026-07-01 · 该不该辞职')")

    response = client.get("/chat")

    assert "2026-07-01 · 该不该辞职" in response.text


def test_chat_new_renders_empty_session_form(client):
    response = client.get("/chat/new")
    assert response.status_code == 200
    assert 'id="chat-send"' in response.text


def test_chat_session_view_renders_history_and_markdown(client):
    db = client.app.state.db
    session_id = db.execute(
        "INSERT INTO chat_session (title) VALUES ('t')"
    ).lastrowid
    db.execute(
        "INSERT INTO chat_message (thread_kind, session_id, role, content) VALUES ('general', ?, 'user', ?)",
        (session_id, "我是不是一直在逃避"),
    )
    db.execute(
        "INSERT INTO chat_message (thread_kind, session_id, role, content) VALUES ('general', ?, 'assistant', ?)",
        (session_id, "**这是重点**"),
    )

    response = client.get(f"/chat/{session_id}")

    assert response.status_code == 200
    assert "<strong>这是重点</strong>" in response.text
    assert "我是不是一直在逃避" in response.text


def test_chat_session_view_404_for_missing_session(client):
    response = client.get("/chat/9999")
    assert response.status_code == 404


def test_chat_rename(client):
    db = client.app.state.db
    session_id = db.execute("INSERT INTO chat_session (title) VALUES ('old')").lastrowid

    response = client.post(f"/chat/{session_id}/rename", json={"title": "new title"})

    assert response.status_code == 200
    row = db.execute("SELECT title FROM chat_session WHERE id = ?", (session_id,)).fetchone()
    assert row["title"] == "new title"


def test_chat_delete_removes_session_and_its_messages(client):
    db = client.app.state.db
    session_id = db.execute("INSERT INTO chat_session (title) VALUES ('t')").lastrowid
    db.execute(
        "INSERT INTO chat_message (thread_kind, session_id, role, content) VALUES ('general', ?, 'user', 'x')",
        (session_id,),
    )

    response = client.post(f"/chat/{session_id}/delete")

    assert response.status_code == 200
    assert db.execute("SELECT * FROM chat_session WHERE id = ?", (session_id,)).fetchone() is None
    assert db.execute("SELECT * FROM chat_message WHERE session_id = ?", (session_id,)).fetchone() is None


def test_new_session_message_lazily_creates_session_and_generates_title(client, monkeypatch):
    monkeypatch.setattr(llm_module, "general_chat_reply", _fake_general_tokens)
    monkeypatch.setattr(llm_module, "generate_session_title", _fake_title)
    db = client.app.state.db
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    )
    assert db.execute("SELECT COUNT(*) AS c FROM chat_session").fetchone()["c"] == 0

    response = client.post("/chat/message", json={"message": "我是不是该辞职"})

    assert response.status_code == 200
    assert "event: done" in response.text
    sessions = db.execute("SELECT id, title FROM chat_session").fetchall()
    assert len(sessions) == 1
    assert "该不该辞职" in sessions[0]["title"]
    rows = db.execute(
        "SELECT role, content, session_id FROM chat_message WHERE thread_kind='general' ORDER BY id"
    ).fetchall()
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[1]["content"] == "从全局看"
    assert all(r["session_id"] == sessions[0]["id"] for r in rows)

    # The done payload carries the new session_id + generated title, mirroring workshop's
    # done-payload mechanism (see routes/workshop.py).
    done_line = [l for l in response.text.splitlines() if l.startswith("data: ") and "session_id" in l][0]
    payload = json.loads(done_line[len("data: "):])
    assert payload["session_id"] == sessions[0]["id"]
    assert "该不该辞职" in payload["title"]


def test_new_session_message_falls_back_to_date_title_on_generation_failure(client, monkeypatch):
    monkeypatch.setattr(llm_module, "general_chat_reply", _fake_general_tokens)
    monkeypatch.setattr(llm_module, "generate_session_title", _failing_title)
    db = client.app.state.db

    response = client.post("/chat/message", json={"message": "随便聊聊"})

    assert response.status_code == 200
    session = db.execute("SELECT title FROM chat_session").fetchone()
    # Falls back to a plain date-only title (no "·" summary suffix) rather than blocking the reply.
    assert "·" not in session["title"]
    assert len(session["title"]) == 10  # "YYYY-MM-DD"


def test_existing_session_message_persists_and_replies(client, monkeypatch):
    monkeypatch.setattr(llm_module, "general_chat_reply", _fake_general_tokens)
    db = client.app.state.db
    session_id = db.execute("INSERT INTO chat_session (title) VALUES ('t')").lastrowid

    response = client.post(f"/chat/{session_id}/message", json={"message": "继续"})

    assert response.status_code == 200
    rows = db.execute(
        "SELECT role, content FROM chat_message WHERE session_id = ? ORDER BY id", (session_id,)
    ).fetchall()
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[1]["content"] == "从全局看"


def test_existing_session_message_404_for_missing_session(client, monkeypatch):
    monkeypatch.setattr(llm_module, "general_chat_reply", _fake_general_tokens)
    response = client.post("/chat/9999/message", json={"message": "x"})
    assert response.status_code == 404
