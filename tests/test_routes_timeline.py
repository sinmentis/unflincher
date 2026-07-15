def test_timeline_lists_entries_newest_first(client):
    db = client.app.state.db
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('老日记', '<p>a</p>', '<p>a</p>', 'a', '2020-01-01', 'import')"
    )
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('新日记', '<p>b</p>', '<p>b</p>', 'b', '2026-01-01', 'import')"
    )

    response = client.get("/")

    assert response.status_code == 200
    body = response.text
    assert body.index("新日记") < body.index("老日记")


def test_timeline_shows_commentary_badge(client):
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('已点评', '<p>a</p>', '<p>a</p>', 'a', '2026-01-01', 'import')"
    ).lastrowid
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) VALUES (2, 'p', 'test-model', 0)"
    ).lastrowid
    db.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status) "
        "VALUES (?, ?, 'm', 'take', 'ok')",
        (entry_id, prompt_id),
    )
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('未点评', '<p>b</p>', '<p>b</p>', 'b', '2026-01-02', 'import')"
    )

    body = client.get("/").text

    assert "Reflected" in body
    assert "Not reflected" in body


def test_timeline_provides_year_sidebar_data(client):
    db = client.app.state.db
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('a', '<p>a</p>', '<p>a</p>', 'a', '2024-03-01', 'import')"
    )
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('b', '<p>b</p>', '<p>b</p>', 'b', '2024-08-01', 'import')"
    )
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('c', '<p>c</p>', '<p>c</p>', 'c', '2023-01-01', 'import')"
    )

    body = client.get("/").text

    # Sidebar shows newest year first, with a per-year entry count.
    assert body.index("2024") < body.index("2023")
    assert 'data-year-link="2024"' in body
    assert 'data-year-count="2"' in body
    assert 'data-year-link="2023"' in body
    assert 'data-year-count="1"' in body
    # Year-divider markup appears once per year, ahead of that year's entries.
    assert body.count('class="archive-year"') == 2


def test_timeline_tags_each_entry_row_with_its_year(client):
    db = client.app.state.db
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('a', '<p>a</p>', '<p>a</p>', 'a', '2024-03-01', 'import')"
    )

    body = client.get("/").text

    assert 'data-year="2024"' in body


def test_timeline_year_density_is_bounded(client):
    db = client.app.state.db
    for title, date in (("a", "2024-03-01"), ("b", "2024-08-01"), ("c", "2023-01-01")):
        db.execute(
            "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
            "entry_date, source) VALUES (?, '<p>x</p>', '<p>x</p>', 'x', ?, 'import')",
            (title, date),
        )
    body = client.get("/").text
    assert 'data-density="3"' in body
    assert 'data-density="2"' in body
    assert "--dot-scale" not in body


def test_timeline_shows_generating_badge_for_entry_with_active_job(client):
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('生成中的', '<p>a</p>', '<p>a</p>', 'a', '2026-01-01', 'import')"
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

    body = client.get("/").text

    assert "Generating" in body


def test_timeline_renders_quiet_archive_with_role_hooks(client):
    db = client.app.state.db
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('First', '<p>x</p>', '<p>x</p>', 'x', '2020-01-01', 'import')"
    )
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('Latest', '<p>x</p>', '<p>x</p>', 'x', '2026-07-13', 'manual')"
    )
    body = client.get("/").text
    assert 'data-role="primary-task"' in body
    assert 'data-role="year-filter"' in body
    assert 'data-role="archive-index"' in body
    assert body.count('data-role="entry-row"') == 2
    assert 'data-entry-count="2"' in body
    assert "2020-01-01" in body and "2026-07-13" in body
    assert 'class="status-mark"' in body
    assert 'class="archive-sequence"' not in body
    assert 'src="/static/js/timeline.js"' in body


def test_timeline_empty_state_explains_both_entry_paths(client):
    body = client.get("/").text
    assert "Douban" in body
    assert 'href="/new"' in body


# ---------------------------------------------------------------------------
# Lightweight, data-derived onboarding (workstream 7). No wizard, tutorial table, cookie,
# localStorage key, or persisted "seen onboarding" flag anywhere -- every state below is derived
# straight from diary_entry/entry_commentary/aggregate_report rows on each render.
# ---------------------------------------------------------------------------

def test_no_entries_offers_exactly_import_and_write_actions(client):
    response = client.get("/")
    body = response.text
    assert 'data-role="onboarding-panel"' not in body
    assert 'href="https://github.com/sinmentis/unflincher/blob/main/docs/import.md"' in body
    assert "Import an existing archive" in body
    assert "Write the first entry" in body
    assert 'href="/new"' in body
    # No unsupported browser-upload or generic import-format claims (Locked decision 8 / plan
    # Non-goal: "In-app Excel upload or a generic multi-format import system").
    assert 'type="file"' not in body
    assert "Day One" not in body
    assert "Notion" not in body
    assert "Google Docs" not in body


def test_entries_with_no_generation_shows_three_step_onboarding_panel(client):
    db = client.app.state.db
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>a</p>', '<p>a</p>', 'a', '2026-01-01', 'manual')"
    )

    body = client.get("/").text

    assert 'data-role="onboarding-panel"' in body
    assert "Analyst is active by default" in body
    panel = body[body.index('data-role="onboarding-panel"'):body.index("archive-index")]
    assert panel.count('href="/workshop"') == 2
    assert 'href="/report"' in panel


def test_failed_only_entry_reflection_keeps_onboarding_panel_visible(client):
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>a</p>', '<p>a</p>', 'a', '2026-01-01', 'manual')"
    ).lastrowid
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) VALUES (2, 'p', 'm', 0)"
    ).lastrowid
    db.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status, error) "
        "VALUES (?, ?, 'm', '', 'failed', 'boom')",
        (entry_id, prompt_id),
    )

    body = client.get("/").text

    assert 'data-role="onboarding-panel"' in body


def test_failed_only_life_report_keeps_onboarding_panel_visible(client):
    db = client.app.state.db
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>a</p>', '<p>a</p>', 'a', '2026-01-01', 'manual')"
    )
    prompt_id = db.execute(
        "SELECT id FROM persona_prompt WHERE is_active = 1"
    ).fetchone()["id"]
    db.execute(
        "INSERT INTO aggregate_report (prompt_version_id, model, body_text, covered_entry_count, "
        "status, error) VALUES (?, 'm', '', 0, 'failed', 'boom')",
        (prompt_id,),
    )

    body = client.get("/").text

    assert 'data-role="onboarding-panel"' in body


def test_successful_entry_reflection_removes_onboarding_panel(client):
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>a</p>', '<p>a</p>', 'a', '2026-01-01', 'manual')"
    ).lastrowid
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) VALUES (2, 'p', 'm', 0)"
    ).lastrowid
    db.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status) "
        "VALUES (?, ?, 'm', 'take', 'ok')",
        (entry_id, prompt_id),
    )

    body = client.get("/").text

    assert 'data-role="onboarding-panel"' not in body
    # Normal Timeline still renders (onboarding never blocks browsing).
    assert 'data-role="archive-index"' in body


def test_successful_life_report_removes_onboarding_panel(client):
    db = client.app.state.db
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>a</p>', '<p>a</p>', 'a', '2026-01-01', 'manual')"
    )
    prompt_id = db.execute(
        "SELECT id FROM persona_prompt WHERE is_active = 1"
    ).fetchone()["id"]
    db.execute(
        "INSERT INTO aggregate_report (prompt_version_id, model, body_text, covered_entry_count, "
        "status) VALUES (?, 'm', 'body', 1, 'ok')",
        (prompt_id,),
    )

    body = client.get("/").text

    assert 'data-role="onboarding-panel"' not in body


def test_onboarding_never_blocks_navigation_writing_or_workshop(client):
    """Onboarding is purely informational -- confirm the surfaces it links to remain fully
    reachable regardless of onboarding stage (empty archive here)."""
    assert client.get("/new").status_code == 200
    assert client.get("/workshop").status_code == 200
    assert client.get("/report").status_code == 200
    assert client.get("/chat").status_code == 200


def test_onboarding_has_no_persisted_state(client):
    db = client.app.state.db
    tables = {
        row["name"] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    forbidden_tables = {
        "onboarding", "onboarding_state", "onboarding_flag",
        "tutorial", "tutorial_state", "wizard_state",
    }
    assert not (tables & forbidden_tables)

    response = client.get("/")
    assert not any(
        "onboarding" in name.lower() or "tutorial" in name.lower()
        for name in response.cookies
    )

    # Re-rendering the SAME state twice produces the SAME onboarding panel presence -- nothing
    # about a previous request changes what the next one shows.
    first = client.get("/").text
    second = client.get("/").text
    assert ('data-role="onboarding-panel"' in first) == ('data-role="onboarding-panel"' in second)
