import sqlite3

import pytest

from diary.db import (
    complete_job_item,
    get_active_prompt,
    get_commentary_by_id,
    get_connection,
    get_current_commentary,
    get_report_by_id,
    init_schema,
    list_commentary_versions,
    list_report_versions,
    set_active_prompt,
    start_regen_job,
)


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "test.db")
    c = get_connection(db_path)
    init_schema(c)
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
    first_id = set_active_prompt(conn, "v1 persona")
    second_id = set_active_prompt(conn, "v2 persona")

    active = get_active_prompt(conn)
    assert active["id"] == second_id
    assert active["body_text"] == "v2 persona"

    first_row = conn.execute(
        "SELECT is_active FROM persona_prompt WHERE id = ?", (first_id,)
    ).fetchone()
    assert first_row["is_active"] == 0


def test_only_one_active_prompt_allowed_at_db_level(conn):
    set_active_prompt(conn, "v1")
    # Directly trying to force a second active row (bypassing set_active_prompt's own
    # deactivation step) must be rejected by the partial unique index, not just app logic.
    conn.execute(
        "INSERT INTO persona_prompt (version_no, body_text, is_active) VALUES (2, 'v2', 0)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE persona_prompt SET is_active = 1 WHERE version_no = 2"
        )
        conn.execute(
            "INSERT INTO persona_prompt (version_no, body_text, is_active) VALUES (3, 'v3', 1)"
        )


def test_current_commentary_excludes_failed_rows(conn):
    entry_id = conn.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'manual')"
    ).lastrowid
    prompt_id = set_active_prompt(conn, "persona")

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
    prompt_id = set_active_prompt(conn, "persona")
    start_regen_job(conn, prompt_id, entry_ids=[e1, e2])

    with pytest.raises(sqlite3.IntegrityError):
        start_regen_job(conn, prompt_id, entry_ids=[e1, e2])


def test_complete_job_item_is_atomic(conn):
    entry_id = conn.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'manual')"
    ).lastrowid
    prompt_id = set_active_prompt(conn, "persona")
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


def test_list_commentary_versions_newest_first_excludes_nothing(conn):
    entry_id = conn.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'manual')"
    ).lastrowid
    prompt_id = set_active_prompt(conn, "p")
    conn.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status, created_at) "
        "VALUES (?, ?, 'm', 'v1', 'ok', '2026-01-01T00:00:00')", (entry_id, prompt_id),
    )
    conn.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status, created_at) "
        "VALUES (?, ?, 'm', 'v2-failed', 'failed', '2026-01-02T00:00:00')", (entry_id, prompt_id),
    )
    conn.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status, created_at) "
        "VALUES (?, ?, 'm', 'v3', 'ok', '2026-01-03T00:00:00')", (entry_id, prompt_id),
    )

    versions = list_commentary_versions(conn, entry_id)

    # unlike get_current_commentary, the history view shows failed attempts too (so the owner
    # can see "this regen failed on this date") — but ordered newest first.
    assert [v["body_text"] for v in versions] == ["v3", "v2-failed", "v1"]


def test_get_commentary_by_id_returns_a_specific_old_version(conn):
    entry_id = conn.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'manual')"
    ).lastrowid
    prompt_id = set_active_prompt(conn, "p")
    old_id = conn.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status) "
        "VALUES (?, ?, 'm', 'old take', 'ok')", (entry_id, prompt_id),
    ).lastrowid

    row = get_commentary_by_id(conn, old_id)
    assert row["body_text"] == "old take"


def test_list_and_get_report_versions(conn):
    prompt_id = set_active_prompt(conn, "p")
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
