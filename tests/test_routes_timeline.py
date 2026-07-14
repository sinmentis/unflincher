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

    assert "Reviewed" in body
    assert "Not reviewed" in body


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
