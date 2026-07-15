import pytest

from unflincher.db import get_connection, init_schema, migrate_persona_prompt_model
from unflincher.onboarding import (
    ACTIVE,
    IMPORT_DOCS_URL,
    NO_ENTRIES,
    READY_TO_REFLECT,
    get_onboarding_state,
)


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "test.db")
    c = get_connection(db_path)
    init_schema(c)
    migrate_persona_prompt_model(c)
    yield c


def _insert_entry(conn, title="t", entry_date="2026-01-01"):
    return conn.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES (?, '<p>x</p>', '<p>x</p>', 'x', ?, 'manual')",
        (title, entry_date),
    ).lastrowid


def _insert_prompt(conn):
    return conn.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) "
        "VALUES (1, 'p', 'm', 1)"
    ).lastrowid


def test_no_entries_stage_for_empty_archive(conn):
    state = get_onboarding_state(conn)
    assert state.stage == NO_ENTRIES
    assert state.entry_count == 0
    assert state.show_start_panel is False


def test_ready_to_reflect_stage_when_entries_exist_with_no_generation(conn):
    _insert_entry(conn)
    state = get_onboarding_state(conn)
    assert state.stage == READY_TO_REFLECT
    assert state.entry_count == 1
    assert state.show_start_panel is True


def test_failed_only_entry_reflection_does_not_count_as_completed_onboarding(conn):
    entry_id = _insert_entry(conn)
    prompt_id = _insert_prompt(conn)
    conn.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status, error) "
        "VALUES (?, ?, 'm', '', 'failed', 'boom')",
        (entry_id, prompt_id),
    )
    state = get_onboarding_state(conn)
    assert state.stage == READY_TO_REFLECT
    assert state.show_start_panel is True


def test_failed_only_life_report_does_not_count_as_completed_onboarding(conn):
    _insert_entry(conn)
    prompt_id = _insert_prompt(conn)
    conn.execute(
        "INSERT INTO aggregate_report (prompt_version_id, model, body_text, "
        "covered_entry_count, status, error) VALUES (?, 'm', '', 0, 'failed', 'boom')",
        (prompt_id,),
    )
    state = get_onboarding_state(conn)
    assert state.stage == READY_TO_REFLECT
    assert state.show_start_panel is True


def test_successful_entry_reflection_moves_to_active(conn):
    entry_id = _insert_entry(conn)
    prompt_id = _insert_prompt(conn)
    conn.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status) "
        "VALUES (?, ?, 'm', 'take', 'ok')",
        (entry_id, prompt_id),
    )
    state = get_onboarding_state(conn)
    assert state.stage == ACTIVE
    assert state.show_start_panel is False


def test_successful_life_report_moves_to_active(conn):
    _insert_entry(conn)
    prompt_id = _insert_prompt(conn)
    conn.execute(
        "INSERT INTO aggregate_report (prompt_version_id, model, body_text, "
        "covered_entry_count, status) VALUES (?, 'm', 'body', 1, 'ok')",
        (prompt_id,),
    )
    state = get_onboarding_state(conn)
    assert state.stage == ACTIVE
    assert state.show_start_panel is False


def test_active_stage_wins_even_with_additional_failed_rows(conn):
    entry_id = _insert_entry(conn)
    prompt_id = _insert_prompt(conn)
    conn.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status) "
        "VALUES (?, ?, 'm', 'take', 'ok')",
        (entry_id, prompt_id),
    )
    conn.execute(
        "INSERT INTO aggregate_report (prompt_version_id, model, body_text, "
        "covered_entry_count, status, error) VALUES (?, 'm', '', 0, 'failed', 'boom')",
        (prompt_id,),
    )
    state = get_onboarding_state(conn)
    assert state.stage == ACTIVE


def test_import_docs_url_points_at_the_supported_douban_cli_instructions():
    assert IMPORT_DOCS_URL.endswith("docs/import.md")
    assert "github.com" in IMPORT_DOCS_URL
