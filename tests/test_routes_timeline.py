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
    assert body.count('class="year-divider"') == 2


def test_timeline_tags_each_entry_row_with_its_year(client):
    db = client.app.state.db
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('a', '<p>a</p>', '<p>a</p>', 'a', '2024-03-01', 'import')"
    )

    body = client.get("/").text

    assert 'data-year="2024"' in body


def test_timeline_computes_share_correctly(client):
    db = client.app.state.db
    # Insert 2 entries from 2024 and 1 from 2023 (3 total)
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

    # 2024 has 2 of 3 entries: share = 0.67
    # 2023 has 1 of 3 entries: share = 0.33
    assert '--dot-scale: 0.67' in body
    assert '--dot-scale: 0.33' in body


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
