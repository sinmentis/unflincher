import sqlite3

import pytest

from unflincher.db import (
    complete_job_item,
    create_chat_session,
    delete_chat_session,
    fail_job_item,
    get_active_prompt,
    get_chat_session,
    get_connection,
    get_current_commentary,
    get_distinct_entry_days,
    get_entries_with_active_commentary_job,
    get_entry_year_counts,
    get_latest_commentary_job_item,
    get_report_by_id,
    init_schema,
    list_chat_sessions,
    list_report_versions,
    migrate_chat_session,
    migrate_persona_prompt_model,
    migrate_persona_prompt_preset_key,
    rename_chat_session,
    set_active_prompt,
    set_active_prompt_and_start_regen_job,
    start_regen_job,
    start_single_entry_commentary_job,
    touch_chat_session,
)


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "test.db")
    c = get_connection(db_path)
    init_schema(c)
    migrate_persona_prompt_model(c)
    yield c
    c.close()


def test_init_schema_creates_all_tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {r["name"] for r in rows}
    expected = {
        "diary_entry", "persona_prompt", "entry_commentary", "aggregate_report",
        "chat_message", "regen_job", "regen_job_item",
    }
    assert expected.issubset(names)


def test_set_active_prompt_deactivates_previous(conn):
    first_id = set_active_prompt(conn, "v1 persona", "gpt-5.4")
    second_id = set_active_prompt(conn, "v2 persona", "claude-opus-4.8")

    active = get_active_prompt(conn)
    assert active["id"] == second_id
    assert active["body_text"] == "v2 persona"
    assert active["model"] == "claude-opus-4.8"

    first_row = conn.execute(
        "SELECT is_active FROM persona_prompt WHERE id = ?", (first_id,)
    ).fetchone()
    assert first_row["is_active"] == 0


def test_set_active_prompt_persists_model(conn):
    pid = set_active_prompt(conn, "人设", "gpt-5.4")
    active = get_active_prompt(conn)
    assert active["id"] == pid
    assert active["model"] == "gpt-5.4"


def test_set_active_prompt_derives_preset_key_from_exact_shipped_preset_text(conn):
    """The server derives preset identity from EXACT body text via
    perspectives.classify_prompt(). set_active_prompt accepts an optional preset_key parameter
    (see test_set_active_prompt_ignores_a_forged_preset_key_hint_for_exact_analyst_text), but
    never trusts it -- a stale or forged claim can never misclassify edited text (see the plan's
    Persistence and migration section, item 6)."""
    from unflincher.perspectives import get_preset

    analyst = get_preset("analyst")
    pid = set_active_prompt(conn, analyst.prompt, "gpt-5.4")
    active = get_active_prompt(conn)
    assert active["id"] == pid
    assert active["preset_key"] == "analyst"


def test_set_active_prompt_persists_null_preset_key_for_arbitrary_custom_text(conn):
    pid = set_active_prompt(conn, "my own custom instructions", "gpt-5.4")
    active = get_active_prompt(conn)
    assert active["id"] == pid
    assert active["preset_key"] is None


def test_set_active_prompt_persists_null_preset_key_for_an_edited_preset(conn):
    """Even ONE character of drift from the shipped preset text must classify as Custom -- there
    is no fuzzy/partial matching, only exact equality (see perspectives.classify_prompt)."""
    from unflincher.perspectives import get_preset

    analyst = get_preset("analyst")
    edited = analyst.prompt + " (with one extra trailing sentence the owner added)"
    pid = set_active_prompt(conn, edited, "gpt-5.4")
    active = get_active_prompt(conn)
    assert active["id"] == pid
    assert active["preset_key"] is None


def test_set_active_prompt_and_start_regen_job_also_derives_preset_key(conn):
    entry_id = conn.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    from unflincher.perspectives import get_preset

    coach = get_preset("coach")
    prompt_id, _job_id = set_active_prompt_and_start_regen_job(
        conn, coach.prompt, "gpt-5.4", [entry_id]
    )
    active = get_active_prompt(conn)
    assert active["id"] == prompt_id
    assert active["preset_key"] == "coach"


def test_set_active_prompt_ignores_a_forged_preset_key_hint_for_exact_analyst_text(conn):
    """The optional preset_key parameter is a caller-claimed hint only -- a forged/mismatched
    claim can never override the server's own exact-text classification."""
    from unflincher.perspectives import get_preset

    analyst = get_preset("analyst")
    pid = set_active_prompt(conn, analyst.prompt, "gpt-5.4", preset_key="coach")
    active = get_active_prompt(conn)
    assert active["id"] == pid
    assert active["preset_key"] == "analyst"


def test_set_active_prompt_ignores_a_stale_preset_key_hint_for_an_edited_preset(conn):
    from unflincher.perspectives import get_preset

    analyst = get_preset("analyst")
    edited = analyst.prompt + " (edited by the owner)"
    pid = set_active_prompt(conn, edited, "gpt-5.4", preset_key="analyst")
    active = get_active_prompt(conn)
    assert active["id"] == pid
    assert active["preset_key"] is None


def test_set_active_prompt_and_start_regen_job_ignores_a_forged_preset_key_hint(conn):
    entry_id = conn.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    from unflincher.perspectives import get_preset

    coach = get_preset("coach")
    prompt_id, _job_id = set_active_prompt_and_start_regen_job(
        conn, coach.prompt, "gpt-5.4", [entry_id], preset_key="challenger",
    )
    active = get_active_prompt(conn)
    assert active["id"] == prompt_id
    assert active["preset_key"] == "coach"


def test_migration_is_idempotent(tmp_path):
    # Runs against a fresh DB whose persona_prompt was created WITHOUT the model column (the
    # SCHEMA's CREATE TABLE is unchanged); running the migration twice must not error and must
    # leave exactly one model column.
    c = get_connection(str(tmp_path / "m.db"))
    init_schema(c)
    assert "model" not in {r["name"] for r in c.execute("PRAGMA table_info(persona_prompt)")}
    migrate_persona_prompt_model(c)
    migrate_persona_prompt_model(c)  # second run is a no-op, not a duplicate-column error
    model_cols = [r for r in c.execute("PRAGMA table_info(persona_prompt)") if r["name"] == "model"]
    assert len(model_cols) == 1
    c.close()


def test_preset_key_migration_is_idempotent_against_a_pre_workstream_table(tmp_path):
    # Simulates a database created by code that predates the preset_key column: persona_prompt
    # manufactured with the OLD column set (no preset_key), unlike init_schema()'s current SCHEMA
    # which already includes it for brand-new installs.
    c = get_connection(str(tmp_path / "old-preset.db"))
    c.execute(
        "CREATE TABLE persona_prompt (id INTEGER PRIMARY KEY AUTOINCREMENT, version_no INTEGER "
        "NOT NULL, body_text TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 0, model TEXT NOT "
        "NULL DEFAULT 'claude-sonnet-4.6', created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    assert "preset_key" not in {r["name"] for r in c.execute("PRAGMA table_info(persona_prompt)")}
    migrate_persona_prompt_preset_key(c)
    migrate_persona_prompt_preset_key(c)  # second run is a no-op, not a duplicate-column error
    preset_key_cols = [
        r for r in c.execute("PRAGMA table_info(persona_prompt)") if r["name"] == "preset_key"
    ]
    assert len(preset_key_cols) == 1
    c.close()


def test_preset_key_migration_backfills_null_and_touches_nothing_else(tmp_path):
    # A pre-existing row -- even one whose body_text happens to EXACTLY equal a current shipped
    # preset -- must receive preset_key IS NULL from this migration, never retrospectively
    # classified (see item 6/2's "existing migrated rows are never retrospectively classified").
    from unflincher.perspectives import get_preset

    analyst = get_preset("analyst")
    c = get_connection(str(tmp_path / "old-preset-rows.db"))
    c.execute(
        "CREATE TABLE persona_prompt (id INTEGER PRIMARY KEY AUTOINCREMENT, version_no INTEGER "
        "NOT NULL, body_text TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 0, model TEXT NOT "
        "NULL DEFAULT 'claude-sonnet-4.6', created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    c.execute(
        "INSERT INTO persona_prompt (version_no, body_text, is_active, model, created_at) "
        "VALUES (1, ?, 1, 'gpt-5.4', '2025-01-01T00:00:00+00:00')",
        (analyst.prompt,),
    )
    migrate_persona_prompt_preset_key(c)
    row = c.execute("SELECT * FROM persona_prompt WHERE version_no = 1").fetchone()
    assert row["preset_key"] is None  # never retrospectively classified
    assert row["body_text"] == analyst.prompt  # byte-for-byte unchanged
    assert row["is_active"] == 1
    assert row["model"] == "gpt-5.4"
    assert row["created_at"] == "2025-01-01T00:00:00+00:00"
    c.close()


def test_migration_backfills_existing_old_schema_rows(tmp_path):
    # Simulates the live production DB: a persona_prompt row written the OLD way (before the
    # column existed) must backfill to the default model when the migration runs on startup.
    c = get_connection(str(tmp_path / "old.db"))
    init_schema(c)
    c.execute(
        "INSERT INTO persona_prompt (version_no, body_text, is_active) VALUES (1, '线上人设', 1)"
    )
    migrate_persona_prompt_model(c)
    row = c.execute("SELECT model FROM persona_prompt WHERE version_no = 1").fetchone()
    assert row["model"] == "claude-sonnet-4.6"
    c.close()


def test_only_one_active_prompt_allowed_at_db_level(conn):
    set_active_prompt(conn, "v1", "test-model")
    # Directly trying to force a second active row (bypassing set_active_prompt's own
    # deactivation step) must be rejected by the partial unique index, not just app logic.
    conn.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) VALUES (2, 'v2', 'test-model', 0)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE persona_prompt SET is_active = 1 WHERE version_no = 2"
        )
        conn.execute(
            "INSERT INTO persona_prompt (version_no, body_text, model, is_active) VALUES (3, 'v3', 'test-model', 1)"
        )


def test_current_commentary_excludes_failed_rows(conn):
    entry_id = conn.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'manual')"
    ).lastrowid
    prompt_id = set_active_prompt(conn, "persona", "test-model")

    conn.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status, created_at) "
        "VALUES (?, ?, 'test-model', 'good take', 'ok', '2026-01-01T00:00:00')",
        (entry_id, prompt_id),
    )
    conn.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status, error, created_at) "
        "VALUES (?, ?, 'test-model', '', 'failed', 'boom', '2026-01-02T00:00:00')",
        (entry_id, prompt_id),
    )
    conn.commit()

    current = get_current_commentary(conn, entry_id)
    assert current is not None
    assert current["body_text"] == "good take"
    assert current["status"] == "ok"


def test_start_regen_job_rejects_second_concurrent_job(conn):
    # Seed two real diary entries: regen_job_item.entry_id has an FK to diary_entry(id)
    # and get_connection() runs with foreign_keys=ON, so the entry ids passed to
    # start_regen_job must reference real rows. The invariant under test is the
    # "only one running job" partial unique index, not FK behaviour.
    e1 = conn.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t1', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'manual')"
    ).lastrowid
    e2 = conn.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t2', '<p>y</p>', '<p>y</p>', 'y', '2026-01-02', 'manual')"
    ).lastrowid
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    start_regen_job(conn, prompt_id, entry_ids=[e1, e2])

    with pytest.raises(sqlite3.IntegrityError):
        start_regen_job(conn, prompt_id, entry_ids=[e1, e2])


def test_atomic_prompt_and_regen_job_rolls_back_prompt_when_busy(conn):
    original_id = set_active_prompt(conn, "original", "test-model")
    start_regen_job(conn, original_id, [])
    before_count = conn.execute("SELECT COUNT(*) AS n FROM persona_prompt").fetchone()["n"]

    with pytest.raises(sqlite3.IntegrityError):
        set_active_prompt_and_start_regen_job(
            conn,
            "must roll back",
            "other-model",
            [],
        )

    active = get_active_prompt(conn)
    after_count = conn.execute("SELECT COUNT(*) AS n FROM persona_prompt").fetchone()["n"]
    assert active["id"] == original_id
    assert active["body_text"] == "original"
    assert after_count == before_count


def test_complete_job_item_is_atomic(conn):
    entry_id = conn.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'manual')"
    ).lastrowid
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    job_id = start_regen_job(conn, prompt_id, entry_ids=[entry_id])
    item_id = conn.execute(
        "SELECT id FROM regen_job_item WHERE job_id = ? AND entry_id = ?", (job_id, entry_id)
    ).fetchone()["id"]

    complete_job_item(
        conn, item_id, "entry_commentary",
        {
            "entry_id": entry_id, "prompt_version_id": prompt_id, "model": "test-model",
            "body_text": "generated", "status": "ok", "created_at": "2026-01-01T00:00:01",
        },
    )

    item = conn.execute("SELECT status, result_id FROM regen_job_item WHERE id = ?", (item_id,)).fetchone()
    assert item["status"] == "ok"
    assert item["result_id"] is not None
    commentary = conn.execute(
        "SELECT body_text FROM entry_commentary WHERE id = ?", (item["result_id"],)
    ).fetchone()
    assert commentary["body_text"] == "generated"


def test_complete_job_item_prunes_older_entry_commentary_rows(conn):
    """Entries only ever keep their latest reflection -- completing a new entry_commentary job
    item must delete every OTHER row for that entry_id in the same transaction."""
    entry_id = conn.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'manual')"
    ).lastrowid
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    old_id = conn.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status, created_at) "
        "VALUES (?, ?, 'm', 'old take', 'ok', '2026-01-01T00:00:00')", (entry_id, prompt_id),
    ).lastrowid

    job_id = start_regen_job(conn, prompt_id, entry_ids=[entry_id])
    item_id = conn.execute(
        "SELECT id FROM regen_job_item WHERE job_id = ? AND entry_id = ?", (job_id, entry_id)
    ).fetchone()["id"]
    complete_job_item(
        conn, item_id, "entry_commentary",
        {
            "entry_id": entry_id, "prompt_version_id": prompt_id, "model": "test-model",
            "body_text": "new take", "status": "ok", "created_at": "2026-01-02T00:00:00",
        },
    )

    rows = conn.execute(
        "SELECT id, body_text FROM entry_commentary WHERE entry_id = ?", (entry_id,)
    ).fetchall()
    assert [r["body_text"] for r in rows] == ["new take"]
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM entry_commentary WHERE id = ?", (old_id,)
    ).fetchone()["n"] == 0


def test_complete_job_item_never_prunes_aggregate_report(conn):
    """Unlike entry_commentary, aggregate_report keeps its full version history -- completing a
    new report job item must never delete a prior report row."""
    prompt_id = set_active_prompt(conn, "persona", "test-model")
    old_id = conn.execute(
        "INSERT INTO aggregate_report (prompt_version_id, model, body_text, covered_entry_count, "
        "status, created_at) VALUES (?, 'm', 'old report', 1, 'ok', '2026-01-01T00:00:00')",
        (prompt_id,),
    ).lastrowid

    job_id = start_regen_job(conn, prompt_id, entry_ids=[])
    item_id = conn.execute(
        "SELECT id FROM regen_job_item WHERE job_id = ? AND target_type = 'aggregate_report'",
        (job_id,),
    ).fetchone()["id"]
    complete_job_item(
        conn, item_id, "aggregate_report",
        {
            "prompt_version_id": prompt_id, "model": "test-model", "body_text": "new report",
            "covered_entry_count": 0, "covered_from_date": None, "covered_to_date": None,
            "status": "ok", "created_at": "2026-01-02T00:00:00",
        },
    )

    rows = conn.execute("SELECT id, body_text FROM aggregate_report ORDER BY id").fetchall()
    assert [r["body_text"] for r in rows] == ["old report", "new report"]
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM aggregate_report WHERE id = ?", (old_id,)
    ).fetchone()["n"] == 1


def test_list_and_get_report_versions(conn):
    prompt_id = set_active_prompt(conn, "p", "test-model")
    first_id = conn.execute(
        "INSERT INTO aggregate_report (prompt_version_id, model, body_text, covered_entry_count, "
        "status, created_at) VALUES (?, 'm', '第一版', 1, 'ok', '2026-01-01T00:00:00')", (prompt_id,),
    ).lastrowid
    conn.execute(
        "INSERT INTO aggregate_report (prompt_version_id, model, body_text, covered_entry_count, "
        "status, created_at) VALUES (?, 'm', '第二版', 2, 'ok', '2026-01-02T00:00:00')", (prompt_id,),
    )

    versions = list_report_versions(conn)
    assert [v["body_text"] for v in versions] == ["第二版", "第一版"]
    assert get_report_by_id(conn, first_id)["body_text"] == "第一版"


def test_create_and_get_chat_session(conn):
    migrate_chat_session(conn)
    session_id = create_chat_session(conn, "2026-07-10")
    row = get_chat_session(conn, session_id)
    assert row["title"] == "2026-07-10"


def test_list_chat_sessions_orders_by_updated_at_desc(conn):
    migrate_chat_session(conn)
    first = create_chat_session(conn, "first")
    second = create_chat_session(conn, "second")
    touch_chat_session(conn, first)  # bump first back to the top

    rows = list_chat_sessions(conn)

    assert [r["id"] for r in rows] == [first, second]


def test_list_chat_sessions_includes_message_count(conn):
    migrate_chat_session(conn)
    quiet = create_chat_session(conn, "quiet")
    chatty = create_chat_session(conn, "chatty")
    conn.execute(
        "INSERT INTO chat_message (thread_kind, session_id, role, content) VALUES ('general', ?, 'user', 'a')",
        (chatty,),
    )
    conn.execute(
        "INSERT INTO chat_message (thread_kind, session_id, role, content) VALUES ('general', ?, 'assistant', 'b')",
        (chatty,),
    )

    rows = {r["id"]: r["message_count"] for r in list_chat_sessions(conn)}

    assert rows[chatty] == 2
    assert rows[quiet] == 0


def test_rename_chat_session(conn):
    migrate_chat_session(conn)
    session_id = create_chat_session(conn, "old title")
    rename_chat_session(conn, session_id, "new title")
    assert get_chat_session(conn, session_id)["title"] == "new title"


def test_delete_chat_session_removes_its_messages_too(conn):
    migrate_chat_session(conn)
    session_id = create_chat_session(conn, "t")
    other_session_id = create_chat_session(conn, "other")
    conn.execute(
        "INSERT INTO chat_message (thread_kind, session_id, role, content) VALUES ('general', ?, 'user', 'a')",
        (session_id,),
    )
    conn.execute(
        "INSERT INTO chat_message (thread_kind, session_id, role, content) VALUES ('general', ?, 'user', 'b')",
        (other_session_id,),
    )

    delete_chat_session(conn, session_id, "owner-a")

    assert get_chat_session(conn, session_id) is None
    remaining = conn.execute("SELECT session_id FROM chat_message").fetchall()
    assert [r["session_id"] for r in remaining] == [other_session_id]


def test_delete_chat_session_rejects_when_thread_lease_is_busy(conn):
    from unflincher.db import TargetBusyError, acquire_lease, conversation_thread_key

    migrate_chat_session(conn)
    session_id = create_chat_session(conn, "t")
    conn.execute(
        "INSERT INTO chat_message (thread_kind, session_id, role, content) VALUES ('general', ?, 'user', 'a')",
        (session_id,),
    )
    acquire_lease(conn, conversation_thread_key(session_id), "thread", "active-stream")

    with pytest.raises(TargetBusyError):
        delete_chat_session(conn, session_id, "owner-a")

    # No-write 409 semantics: the session and its message are preserved.
    assert get_chat_session(conn, session_id) is not None
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM chat_message WHERE session_id = ?", (session_id,)
    ).fetchone()["n"] == 1


def test_delete_chat_session_leaves_no_dangling_lease_after_success(conn):
    from unflincher.db import conversation_thread_key, get_lease_by_target

    migrate_chat_session(conn)
    session_id = create_chat_session(conn, "t")

    delete_chat_session(conn, session_id, "owner-a")

    assert get_lease_by_target(conn, conversation_thread_key(session_id)) is None


def test_delete_chat_session_works_even_when_maintenance_is_locked(conn):
    from unflincher.db import set_maintenance_locked

    migrate_chat_session(conn)
    session_id = create_chat_session(conn, "t")
    set_maintenance_locked(conn, True)

    # Deletion is cleanup, not new generation work -- it must not be blocked by the maintenance
    # gate the way acquire_lease() blocks new admissions.
    delete_chat_session(conn, session_id, "owner-a")

    assert get_chat_session(conn, session_id) is None


def test_create_general_chat_session_and_convert_lease_happy_path(conn):
    from unflincher.db import (
        acquire_lease,
        conversation_thread_key,
        create_general_chat_session_and_convert_lease,
        get_lease_by_target,
    )

    migrate_chat_session(conn)
    request_lease_id = acquire_lease(conn, "request:abc123", "request", "owner-a")

    session_id = create_general_chat_session_and_convert_lease(
        conn, request_lease_id=request_lease_id, title="2026-07-15", first_message="第一条消息",
    )

    session = get_chat_session(conn, session_id)
    assert session["title"] == "2026-07-15"
    message = conn.execute(
        "SELECT role, content FROM chat_message WHERE session_id = ?", (session_id,)
    ).fetchone()
    assert message["role"] == "user"
    assert message["content"] == "第一条消息"
    # The SAME lease now protects the new conversation thread, not the old request key.
    assert get_lease_by_target(conn, "request:abc123") is None
    lease = get_lease_by_target(conn, conversation_thread_key(session_id))
    assert lease is not None
    assert lease["id"] == request_lease_id


def test_create_general_chat_session_and_convert_lease_raises_when_request_lease_missing(conn):
    from unflincher.db import RequestLeaseExpiredError, create_general_chat_session_and_convert_lease

    migrate_chat_session(conn)
    with pytest.raises(RequestLeaseExpiredError):
        create_general_chat_session_and_convert_lease(
            conn, request_lease_id=999999, title="t", first_message="m",
        )
    assert conn.execute("SELECT COUNT(*) AS n FROM chat_session").fetchone()["n"] == 0


def test_create_general_chat_session_and_convert_lease_does_not_check_maintenance(conn):
    # Deliberate: the request lease itself is proof of prior admission -- this handoff must
    # succeed even if maintenance became locked AFTER the request lease was acquired.
    from unflincher.db import (
        acquire_lease,
        create_general_chat_session_and_convert_lease,
        set_maintenance_locked,
    )

    migrate_chat_session(conn)
    request_lease_id = acquire_lease(conn, "request:abc123", "request", "owner-a")
    set_maintenance_locked(conn, True)

    session_id = create_general_chat_session_and_convert_lease(
        conn, request_lease_id=request_lease_id, title="t", first_message="m",
    )

    assert get_chat_session(conn, session_id) is not None


def test_migrate_chat_session_is_idempotent(tmp_path):
    c = get_connection(str(tmp_path / "m.db"))
    init_schema(c)
    assert "session_id" not in {r["name"] for r in c.execute("PRAGMA table_info(chat_message)")}
    migrate_chat_session(c)
    migrate_chat_session(c)  # second run must not error
    session_cols = [r for r in c.execute("PRAGMA table_info(chat_message)") if r["name"] == "session_id"]
    assert len(session_cols) == 1
    c.close()


def test_migrate_chat_session_discards_old_general_thread_only_once(tmp_path):
    # Simulates the live production DB: pre-existing single-thread general-chat rows (predating
    # chat_session entirely) must be discarded when the column is added, per the explicit product
    # decision not to migrate that old thread into a "session 1". Per-entry rows are untouched.
    c = get_connection(str(tmp_path / "old.db"))
    init_schema(c)
    c.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('e', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    )
    c.execute("INSERT INTO chat_message (thread_kind, role, content) VALUES ('general', 'user', '旧的总对话')")
    c.execute("INSERT INTO chat_message (thread_kind, entry_id, role, content) VALUES ('entry', 1, 'user', '逐篇对话')")

    migrate_chat_session(c)

    rows = c.execute("SELECT thread_kind FROM chat_message").fetchall()
    assert [r["thread_kind"] for r in rows] == ["entry"]

    # Adding a NEW general row post-migration, then re-running the (now idempotent) migration,
    # must NOT discard it — the DELETE only fires the one time the column is actually being added.
    session_id = create_chat_session(c, "new session")
    c.execute(
        "INSERT INTO chat_message (thread_kind, session_id, role, content) VALUES ('general', ?, 'user', '新对话')",
        (session_id,),
    )
    migrate_chat_session(c)
    rows = c.execute("SELECT thread_kind FROM chat_message").fetchall()
    assert len(rows) == 2
    c.close()



def _seed_entry(conn, title="e", entry_date="2026-01-01"):
    return conn.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES (?, '<p>x</p>', '<p>x</p>', 'x', ?, 'import')",
        (title, entry_date),
    ).lastrowid


def test_start_single_entry_commentary_job_creates_exactly_one_item(conn):
    prompt_id = set_active_prompt(conn, "人设", "test-model")
    entry_id = _seed_entry(conn)
    job_id = start_single_entry_commentary_job(conn, prompt_id, entry_id)

    items = conn.execute("SELECT * FROM regen_job_item WHERE job_id = ?", (job_id,)).fetchall()
    assert len(items) == 1
    assert items[0]["target_type"] == "entry_commentary"
    assert items[0]["entry_id"] == entry_id
    assert items[0]["status"] == "pending"

    job = conn.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    assert job["status"] == "running"


def test_start_single_entry_commentary_job_raises_if_a_job_is_already_running(conn):
    prompt_id = set_active_prompt(conn, "人设", "test-model")
    entry_id_1 = _seed_entry(conn, "a")
    entry_id_2 = _seed_entry(conn, "b")
    start_single_entry_commentary_job(conn, prompt_id, entry_id_1)

    with pytest.raises(sqlite3.IntegrityError):
        start_single_entry_commentary_job(conn, prompt_id, entry_id_2)


def test_get_entries_with_active_commentary_job(conn):
    prompt_id = set_active_prompt(conn, "人设", "test-model")
    entry_id_pending = _seed_entry(conn, "a")
    entry_id_running = _seed_entry(conn, "b")
    entry_id_ok = _seed_entry(conn, "c")
    job_id = conn.execute(
        "INSERT INTO regen_job (prompt_version_id, status) VALUES (?, 'running')", (prompt_id,)
    ).lastrowid
    conn.execute(
        "INSERT INTO regen_job_item (job_id, target_type, entry_id, status) "
        "VALUES (?, 'entry_commentary', ?, 'pending')", (job_id, entry_id_pending),
    )
    conn.execute(
        "INSERT INTO regen_job_item (job_id, target_type, entry_id, status) "
        "VALUES (?, 'entry_commentary', ?, 'running')", (job_id, entry_id_running),
    )
    conn.execute(
        "INSERT INTO regen_job_item (job_id, target_type, entry_id, status) "
        "VALUES (?, 'entry_commentary', ?, 'ok')", (job_id, entry_id_ok),
    )

    assert get_entries_with_active_commentary_job(conn) == {entry_id_pending, entry_id_running}


def test_get_latest_commentary_job_item_returns_none_when_never_triggered(conn):
    entry_id = _seed_entry(conn)
    assert get_latest_commentary_job_item(conn, entry_id) is None


def test_get_latest_commentary_job_item_returns_the_newest_one(conn):
    prompt_id = set_active_prompt(conn, "人设", "test-model")
    entry_id = _seed_entry(conn)
    job_id = conn.execute(
        "INSERT INTO regen_job (prompt_version_id, status) VALUES (?, 'done')", (prompt_id,)
    ).lastrowid
    old_item_id = conn.execute(
        "INSERT INTO regen_job_item (job_id, target_type, entry_id, status) "
        "VALUES (?, 'entry_commentary', ?, 'failed')", (job_id, entry_id),
    ).lastrowid
    fail_job_item(conn, old_item_id, "旧的失败原因")
    new_item_id = conn.execute(
        "INSERT INTO regen_job_item (job_id, target_type, entry_id, status) "
        "VALUES (?, 'entry_commentary', ?, 'ok')", (job_id, entry_id),
    ).lastrowid

    latest = get_latest_commentary_job_item(conn, entry_id)
    assert latest["id"] == new_item_id
    assert latest["status"] == "ok"


def test_get_entry_year_counts_groups_by_year_newest_first(conn):
    _seed_entry(conn, "a", entry_date="2024-03-01")
    _seed_entry(conn, "b", entry_date="2024-08-01")
    _seed_entry(conn, "c", entry_date="2023-01-01")

    counts = get_entry_year_counts(conn)

    assert counts == [{"year": "2024", "count": 2}, {"year": "2023", "count": 1}]


def test_get_entry_year_counts_is_empty_for_a_fresh_archive(conn):
    assert get_entry_year_counts(conn) == []


def test_get_distinct_entry_days_dedupes_same_day_entries_newest_first(conn):
    _seed_entry(conn, "a", entry_date="2024-03-01T08:00:00+00:00")
    _seed_entry(conn, "b", entry_date="2024-03-01T20:00:00+00:00")  # same day, second entry
    _seed_entry(conn, "c", entry_date="2024-03-02T00:00:00+00:00")

    days = get_distinct_entry_days(conn)

    assert days == ["2024-03-02", "2024-03-01"]


def test_get_distinct_entry_days_is_empty_for_a_fresh_archive(conn):
    assert get_distinct_entry_days(conn) == []
