def test_entry_detail_shows_content(client):
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('标题', '<p>正文</p>', '<p>正文</p>', '正文', "
        "'2026-01-01', 'import')"
    ).lastrowid

    response = client.get(f"/entry/{entry_id}")

    assert response.status_code == 200
    assert "标题" in response.text
    assert "<p>正文</p>" in response.text


def test_entry_detail_shows_current_commentary_when_present(client):
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('标题', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, is_active) VALUES (1, 'p', 1)"
    ).lastrowid
    db.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status) "
        "VALUES (?, ?, 'm', '**这是锐评**', 'ok')",
        (entry_id, prompt_id),
    )

    response = client.get(f"/entry/{entry_id}")

    assert "<strong>这是锐评</strong>" in response.text


def test_entry_detail_shows_pending_state_when_no_commentary(client):
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('标题', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid

    response = client.get(f"/entry/{entry_id}")

    assert "还没有锐评" in response.text


def test_entry_detail_404_for_missing_entry(client):
    response = client.get("/entry/9999")
    assert response.status_code == 404
