def test_base_nav_switches_with_cookie(client):
    client.cookies.set("unflincher_lang", "ja")
    res = client.get("/")
    assert "タイムライン" in res.text
    assert "人生レポート" in res.text
    assert "時間線" not in res.text  # old hardcoded Chinese must be gone


def test_base_nav_defaults_to_english_with_no_cookie(client):
    res = client.get("/")
    assert "Timeline" in res.text
    assert "Life Report" in res.text


def test_timeline_badges_translate(client, tmp_path):
    import sqlite3

    conn: sqlite3.Connection = client.app.state.db
    conn.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, entry_date, source) "
        "VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2024-01-01T00:00:00', 'manual')"
    )
    conn.commit()
    client.cookies.set("unflincher_lang", "fr")
    res = client.get("/")
    assert "Non commenté" in res.text


def test_entry_detail_translates(client):
    import sqlite3

    conn: sqlite3.Connection = client.app.state.db
    cur = conn.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, entry_date, source) "
        "VALUES ('t', '<p>x</p>', '<p>x</p>', 'x', '2024-01-01T00:00:00', 'manual')"
    )
    conn.commit()
    entry_id = cur.lastrowid
    client.cookies.set("unflincher_lang", "de")
    res = client.get(f"/entry/{entry_id}")
    assert "Noch kein Kommentar." in res.text
    assert "KI-Kommentar" in res.text
