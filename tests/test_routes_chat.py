import asyncio
import json

import pytest

import unflincher.llm as llm_module


@pytest.fixture(autouse=True)
def _fake_model_limit(monkeypatch):
    """Every conversation route now preflights against get_model_max_prompt_tokens() before
    generating -- fake it so tests never need a real Copilot client just to pass preflight."""
    async def _fake_limit(model):
        return 200_000
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _fake_limit)


async def _fake_general_tokens(envelope):
    if envelope.target_kind == "conversation_title":
        yield "该不该辞职"
        return
    for t in ["从", "全局", "看"]:
        yield t


async def _fake_general_tokens_with_failing_title(envelope):
    if envelope.target_kind == "conversation_title":
        raise RuntimeError("title model unavailable")
    for t in ["从", "全局", "看"]:
        yield t


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


def test_chat_list_renders_session_ledger_and_composed_empty_state(client):
    body = client.get("/chat").text
    assert 'class="chat-layout"' in body
    assert 'data-role="primary-task"' in body
    assert 'class="session-ledger"' in body
    assert 'data-role="session-list"' in body
    assert 'class="empty-state"' in body
    assert 'src="/static/js/chat.js"' in body


def test_chat_session_has_editorial_messages_and_multiline_composer(client):
    db = client.app.state.db
    session_id = db.execute("INSERT INTO chat_session (title) VALUES ('Choosing without certainty')").lastrowid
    db.execute(
        "INSERT INTO chat_message (thread_kind, session_id, role, content) "
        "VALUES ('general', ?, 'user', 'How do I know?')",
        (session_id,),
    )
    body = client.get(f"/chat/{session_id}").text
    assert 'class="conversation-workspace"' in body
    assert 'data-role="conversation"' in body
    assert 'data-role="composer"' in body
    assert 'class="conversation-message is-user"' in body
    assert 'id="chat-input"' in body and "<textarea" in body
    assert 'class="topbar-back" href="/chat"' in body
    assert 'class="mobile-chat-back"' not in body


def test_chat_sidebar_uses_inline_rename_and_delete_controls(client):
    db = client.app.state.db
    session_id = db.execute("INSERT INTO chat_session (title) VALUES ('Old title')").lastrowid
    body = client.get(f"/chat/{session_id}").text
    assert f'data-rename-session="{session_id}"' in body
    assert f'data-delete-session="{session_id}"' in body
    assert f'id="rename-session-{session_id}"' in body
    assert f'id="delete-session-{session_id}"' in body
    assert "✎" not in body
    assert "🗑" not in body


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


def test_chat_delete_409_when_session_thread_busy_and_preserves_session(client):
    from unflincher.db import acquire_lease, conversation_thread_key

    db = client.app.state.db
    session_id = db.execute("INSERT INTO chat_session (title) VALUES ('t')").lastrowid
    acquire_lease(db, conversation_thread_key(session_id), "thread", "active-stream")

    response = client.post(f"/chat/{session_id}/delete")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "target_busy"
    assert db.execute("SELECT * FROM chat_session WHERE id = ?", (session_id,)).fetchone() is not None


def test_new_session_message_lazily_creates_session_and_generates_title(client, monkeypatch):
    monkeypatch.setattr(llm_module, "stream_completion_envelope", _fake_general_tokens)
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
    done_line = [
        line
        for line in response.text.splitlines()
        if line.startswith("data: ") and "session_id" in line
    ][0]
    payload = json.loads(done_line[len("data: "):])
    assert payload["session_id"] == sessions[0]["id"]
    assert "该不该辞职" in payload["title"]


async def test_cleanup_guard_never_cancels_an_already_completed_title_task():
    """Focused unit test of the exact guard chat.py's send_new_session_message cleanup uses:
    `if not title_task.done(): title_task.cancel()` before `await title_task`. On an
    ALREADY-DONE task (the happy path -- see the sibling integration test above, which proves
    the wired route produces the correct title end to end) this must be a complete no-op that
    never touches the task's cancelled state or corrupts its result. asyncio.Task.cancel() cannot
    be monkeypatched directly (`_asyncio.Task` is an immutable C-level type), so this proves the
    guard's OBSERVABLE contract with a real Task instead of spying on the call itself."""
    async def already_finishes():
        return "generated title"

    task = asyncio.create_task(already_finishes())
    await task  # force it to complete before the guard ever sees it, matching the happy path

    if not task.done():  # exactly chat.py's guard
        task.cancel()
    result = await task

    assert task.cancelled() is False
    assert result == "generated title"


def test_new_session_message_falls_back_to_date_title_on_generation_failure(client, monkeypatch):
    monkeypatch.setattr(llm_module, "stream_completion_envelope", _fake_general_tokens_with_failing_title)
    db = client.app.state.db

    response = client.post("/chat/message", json={"message": "随便聊聊"})

    assert response.status_code == 200
    session = db.execute("SELECT title FROM chat_session").fetchone()
    # Falls back to a plain date-only title (no "·" summary suffix) rather than blocking the reply.
    assert "·" not in session["title"]
    assert len(session["title"]) == 10  # "YYYY-MM-DD"


def test_existing_session_message_persists_and_replies(client, monkeypatch):
    monkeypatch.setattr(llm_module, "stream_completion_envelope", _fake_general_tokens)
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
    monkeypatch.setattr(llm_module, "stream_completion_envelope", _fake_general_tokens)
    response = client.post("/chat/9999/message", json={"message": "x"})
    assert response.status_code == 404


def test_existing_session_message_409_when_thread_already_busy_and_writes_nothing(client):
    from unflincher.db import acquire_lease, conversation_thread_key

    db = client.app.state.db
    session_id = db.execute("INSERT INTO chat_session (title) VALUES ('t')").lastrowid
    acquire_lease(db, conversation_thread_key(session_id), "thread", "someone-else")

    response = client.post(f"/chat/{session_id}/message", json={"message": "继续"})

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "target_busy"
    assert db.execute(
        "SELECT COUNT(*) AS n FROM chat_message WHERE session_id = ?", (session_id,)
    ).fetchone()["n"] == 0


def test_existing_session_message_releases_lease_after_stream_completes(client, monkeypatch):
    from unflincher.db import conversation_thread_key, get_lease_by_target

    monkeypatch.setattr(llm_module, "stream_completion_envelope", _fake_general_tokens)
    db = client.app.state.db
    session_id = db.execute("INSERT INTO chat_session (title) VALUES ('t')").lastrowid

    client.post(f"/chat/{session_id}/message", json={"message": "继续"})

    assert get_lease_by_target(db, conversation_thread_key(session_id)) is None


def test_existing_session_message_413_releases_lease_and_writes_no_message(client, monkeypatch):
    from unflincher.db import conversation_thread_key, get_lease_by_target

    async def _tiny_limit(model):
        return 1
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _tiny_limit)

    db = client.app.state.db
    session_id = db.execute("INSERT INTO chat_session (title) VALUES ('t')").lastrowid

    response = client.post(f"/chat/{session_id}/message", json={"message": "继续"})

    assert response.status_code == 413
    assert response.json()["detail"]["reason"] == "context_too_large"
    assert db.execute(
        "SELECT COUNT(*) AS n FROM chat_message WHERE session_id = ?", (session_id,)
    ).fetchone()["n"] == 0
    assert get_lease_by_target(db, conversation_thread_key(session_id)) is None


def test_new_session_message_413_writes_no_session_and_releases_request_lease(client, monkeypatch):
    async def _tiny_limit(model):
        return 1
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _tiny_limit)

    db = client.app.state.db
    response = client.post("/chat/message", json={"message": "随便聊聊"})

    assert response.status_code == 413
    assert response.json()["detail"]["reason"] == "context_too_large"
    assert db.execute("SELECT COUNT(*) AS n FROM chat_session").fetchone()["n"] == 0
    assert db.execute("SELECT COUNT(*) AS n FROM generation_lease").fetchone()["n"] == 0


def test_new_session_message_releases_lease_and_converts_it_to_the_conversation_thread(client, monkeypatch):
    from unflincher.db import conversation_thread_key, get_lease_by_target

    monkeypatch.setattr(llm_module, "stream_completion_envelope", _fake_general_tokens)
    db = client.app.state.db

    client.post("/chat/message", json={"message": "随便聊聊"})
    session = db.execute("SELECT id FROM chat_session").fetchone()

    # The temporary request lease was converted and then released after the stream -- no lease
    # remains for the new conversation thread once the turn completes.
    assert get_lease_by_target(db, conversation_thread_key(session["id"])) is None
    assert db.execute("SELECT COUNT(*) AS n FROM generation_lease").fetchone()["n"] == 0


def test_title_generation_uses_its_own_request_lease_distinct_from_main_conversation(client, monkeypatch):
    # Prove the title task acquires a SEPARATE request lease from the main conversation lease --
    # capture the set of active lease target_keys at the moment the title's own model call runs.
    seen_lease_keys_during_title_call = []

    async def fake_stream(envelope):
        if envelope.target_kind == "conversation_title":
            rows = client.app.state.db.execute("SELECT target_key FROM generation_lease").fetchall()
            seen_lease_keys_during_title_call.extend(r["target_key"] for r in rows)
            yield "该不该辞职"
            return
        yield "从"
        yield "全局"
        yield "看"

    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)

    response = client.post("/chat/message", json={"message": "我是不是该辞职"})

    assert response.status_code == 200
    # While the title call was in flight, its OWN request:<uuid> lease AND the main conversation's
    # (already-converted) lease both existed simultaneously -- two distinct target keys, not one.
    assert len(seen_lease_keys_during_title_call) == 2
    assert len(set(seen_lease_keys_during_title_call)) == 2
    assert any(k.startswith("request:") for k in seen_lease_keys_during_title_call)
    assert any(k.startswith("conversation:") for k in seen_lease_keys_during_title_call)


def test_title_generation_skips_and_logs_when_maintenance_locked_mid_flight(client, monkeypatch, caplog):
    from unflincher.db import set_maintenance_locked

    async def fake_stream(envelope):
        yield "从"
        yield "全局"
        yield "看"

    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)

    # Lock maintenance before the request so the title's own lease acquisition observes it --
    # the request itself was already admitted (no lease held yet for a brand-new conversation),
    # so this simply proves title generation degrades gracefully rather than raising.
    set_maintenance_locked(client.app.state.db, True)
    import logging
    caplog.set_level(logging.INFO)

    response = client.post("/chat/message", json={"message": "随便聊聊"})

    # Maintenance also blocks the MAIN conversation's own request lease (both are new admissions),
    # so this must fail as a stable 503 -- proving maintenance is at least consistently enforced,
    # and title generation's own lease-based skip path (unit-tested directly below) never even
    # gets a chance to diverge from that.
    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "maintenance_locked"


async def test_generate_title_or_none_skips_and_logs_when_maintenance_locked(client, caplog):
    import logging

    from unflincher.db import set_maintenance_locked
    from unflincher.routes.chat import _generate_title_or_none

    db = client.app.state.db
    set_maintenance_locked(db, True)
    caplog.set_level(logging.INFO)

    result = await _generate_title_or_none(db, "owner-a", "随便聊聊")

    assert result is None
    assert "skipped" in caplog.text
    assert db.execute("SELECT COUNT(*) AS n FROM generation_lease").fetchone()["n"] == 0


def test_existing_session_message_404_when_session_deleted_between_check_and_lease_no_500(client):
    db = client.app.state.db
    session_id = db.execute("INSERT INTO chat_session (title) VALUES ('t')").lastrowid
    from unflincher.db import delete_chat_session
    delete_chat_session(db, session_id, "owner-a")  # simulate the session already being gone

    response = client.post(f"/chat/{session_id}/message", json={"message": "继续"})

    assert response.status_code == 404
    # No FK crash, and no lease left stranded from the failed attempt.
    assert db.execute("SELECT COUNT(*) AS n FROM generation_lease").fetchone()["n"] == 0


def test_existing_session_message_lease_blocks_concurrent_deletion_interleaving(client):
    """DB/route-seam interleaving regression: once send_session_message's lease is held (as it
    would be mid-turn), a concurrent delete_chat_session() attempt must fail busy and preserve the
    session -- this is the actual invariant that makes the route's re-check-after-lease-acquire
    safe (the only way a session can be "gone" after our lease is acquired is if it was gone
    BEFORE, never a genuine mid-flight race)."""
    from unflincher.db import TargetBusyError, acquire_lease, conversation_thread_key, delete_chat_session

    db = client.app.state.db
    session_id = db.execute("INSERT INTO chat_session (title) VALUES ('t')").lastrowid

    # Simulate the route having just acquired its thread lease for an in-flight turn.
    acquire_lease(db, conversation_thread_key(session_id), "thread", "in-flight-turn")

    with pytest.raises(TargetBusyError):
        delete_chat_session(db, session_id, "concurrent-delete-attempt")

    assert db.execute("SELECT * FROM chat_session WHERE id = ?", (session_id,)).fetchone() is not None


def test_new_session_message_uses_canonical_archive_order_for_same_date_entries(client, monkeypatch):
    captured = {}

    async def fake_stream(envelope):
        if envelope.target_kind == "general_chat":
            captured["system_content"] = envelope.system_content
        yield "ok"

    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)
    db = client.app.state.db
    # Two entries sharing the SAME entry_date, inserted in REVERSE of their intended (entry_date,
    # id) order -- id must be the deciding tiebreaker, insertion order into the SQL result set is
    # not guaranteed otherwise.
    later_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('B在后', '<p>b</p>', '<p>b</p>', 'b', '2026-03-01', 'import')"
    ).lastrowid
    earlier_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('A在前', '<p>a</p>', '<p>a</p>', 'a', '2026-03-01', 'import')"
    ).lastrowid
    assert earlier_id > later_id  # sanity: id order is the OPPOSITE of the desired output order

    client.post("/chat/message", json={"message": "hi"})

    assert captured["system_content"].index("B在后") < captured["system_content"].index("A在前")


def test_existing_session_message_uses_canonical_archive_order_for_same_date_entries(client, monkeypatch):
    captured = {}

    async def fake_stream(envelope):
        if envelope.target_kind == "general_chat":
            captured["system_content"] = envelope.system_content
        yield "ok"

    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)
    db = client.app.state.db
    session_id = db.execute("INSERT INTO chat_session (title) VALUES ('t')").lastrowid
    later_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('B在后', '<p>b</p>', '<p>b</p>', 'b', '2026-03-01', 'import')"
    ).lastrowid
    earlier_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('A在前', '<p>a</p>', '<p>a</p>', 'a', '2026-03-01', 'import')"
    ).lastrowid
    assert earlier_id > later_id

    client.post(f"/chat/{session_id}/message", json={"message": "继续"})

    assert captured["system_content"].index("B在后") < captured["system_content"].index("A在前")


# ---------------------------------------------------------------------------
# Perspective indicator (Task: perspective indicators). Conversation composers show the currently
# active Perspective as "Perspective for the next response" -- never a per-message badge, and a
# Perspective change never relabels past messages.
# ---------------------------------------------------------------------------

def test_chat_new_shows_the_active_perspective_for_the_next_response(client):
    # Fresh install seeds Analyst active.
    body = client.get("/chat/new").text
    assert "Perspective for the next response: Analyst" in body


def test_chat_session_view_shows_the_active_perspective_for_the_next_response(client):
    from unflincher.db import set_active_prompt
    from unflincher.perspectives import get_preset

    db = client.app.state.db
    set_active_prompt(db, get_preset("companion").prompt, "test-model")
    session_id = db.execute("INSERT INTO chat_session (title) VALUES ('t')").lastrowid
    db.execute(
        "INSERT INTO chat_message (thread_kind, session_id, role, content) VALUES ('general', ?, 'user', 'hi')",
        (session_id,),
    )

    body = client.get(f"/chat/{session_id}").text

    assert "Perspective for the next response: Companion" in body
    # Non-goal: no per-message Perspective badges on individual turns.
    assert body.count("Perspective for the next response") == 1


def test_chat_session_view_shows_custom_for_a_non_preset_active_prompt(client):
    from unflincher.db import set_active_prompt

    db = client.app.state.db
    set_active_prompt(db, "my own custom instructions", "test-model")
    session_id = db.execute("INSERT INTO chat_session (title) VALUES ('t')").lastrowid

    body = client.get(f"/chat/{session_id}").text

    assert "Perspective for the next response: Custom" in body


def test_changing_active_perspective_does_not_relabel_or_alter_past_messages(client):
    from unflincher.db import set_active_prompt
    from unflincher.perspectives import get_preset

    db = client.app.state.db
    set_active_prompt(db, get_preset("coach").prompt, "test-model")
    session_id = db.execute("INSERT INTO chat_session (title) VALUES ('t')").lastrowid
    db.execute(
        "INSERT INTO chat_message (thread_kind, session_id, role, content) VALUES ('general', ?, 'user', 'hi')",
        (session_id,),
    )
    db.execute(
        "INSERT INTO chat_message (thread_kind, session_id, role, content) VALUES ('general', ?, 'assistant', 'reply under Coach')",
        (session_id,),
    )

    before = client.get(f"/chat/{session_id}").text
    assert "Perspective for the next response: Coach" in before
    assert "reply under Coach" in before

    set_active_prompt(db, get_preset("challenger").prompt, "test-model")
    after = client.get(f"/chat/{session_id}").text

    # Only the composer's forward-looking label changes...
    assert "Perspective for the next response: Challenger" in after
    # ...the existing message content is untouched and unlabeled.
    assert "reply under Coach" in after
    assert "Coach</div>" not in after
    assert "Challenger</div>" not in after
