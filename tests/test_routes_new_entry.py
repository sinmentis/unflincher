def test_new_entry_form_renders(client):
    response = client.get("/new")
    assert response.status_code == 200
    assert "写新日记" in response.text


def test_new_entry_saves_as_manual_and_does_not_trigger_commentary(client):
    response = client.post("/new", data={"title": "今天", "content": "写点什么"}, follow_redirects=False)

    assert response.status_code == 303
    db = client.app.state.db
    row = db.execute("SELECT * FROM diary_entry WHERE title = '今天'").fetchone()
    assert row is not None
    assert row["source"] == "manual"
    assert row["content_text"] == "写点什么"
    assert "<p>写点什么</p>" in row["content_html"]
    # saving never auto-triggers analysis (product spec §2/§4)
    commentary = db.execute(
        "SELECT * FROM entry_commentary WHERE entry_id = ?", (row["id"],)
    ).fetchone()
    assert commentary is None


def test_new_entry_escapes_html_in_content(client):
    client.post("/new", data={"title": "t", "content": "<script>alert(1)</script>"}, follow_redirects=False)
    db = client.app.state.db
    row = db.execute("SELECT * FROM diary_entry WHERE title = 't'").fetchone()
    assert "<script>" not in row["content_html"]
