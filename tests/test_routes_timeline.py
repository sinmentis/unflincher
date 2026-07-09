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

    assert "AI 已点评" in body
    assert "未点评" in body
