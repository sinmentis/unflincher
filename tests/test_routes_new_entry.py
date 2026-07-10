def test_new_entry_form_renders(client):
    response = client.get("/new")
    assert response.status_code == 200
    assert "写新日记" in response.text


def test_new_entry_saves_as_manual_and_does_not_trigger_commentary(client):
    response = client.post("/new", json={"title": "今天", "content": "写点什么"})

    assert response.status_code == 200
    assert "entry_id" in response.json()
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
    client.post("/new", json={"title": "t", "content": "<script>alert(1)</script>"})
    db = client.app.state.db
    row = db.execute("SELECT * FROM diary_entry WHERE title = 't'").fetchone()
    assert "<script>" not in row["content_html"]


def test_new_entry_uses_picked_date(client):
    response = client.post(
        "/new", json={"title": "补记", "content": "那天的事", "entry_date": "2020-03-15"}
    )

    assert response.status_code == 200
    db = client.app.state.db
    row = db.execute("SELECT entry_date FROM diary_entry WHERE title = '补记'").fetchone()
    assert row["entry_date"].startswith("2020-03-15T")


def test_new_entry_without_entry_date_field_still_works(client):
    # Backward compatibility: a request with no entry_date at all (e.g. a stale cached page from
    # before this change) must behave exactly as it always has -- today's real timestamp, not a
    # 400 or a missing/garbage entry_date.
    response = client.post("/new", json={"title": "旧客户端", "content": "没带日期字段"})

    assert response.status_code == 200
    db = client.app.state.db
    row = db.execute("SELECT entry_date FROM diary_entry WHERE title = '旧客户端'").fetchone()
    assert row["entry_date"] is not None and len(row["entry_date"]) > 0


def test_new_entry_rejects_malformed_date(client):
    response = client.post(
        "/new", json={"title": "坏日期", "content": "x", "entry_date": "not-a-date"}
    )

    assert response.status_code == 400
    db = client.app.state.db
    assert db.execute("SELECT * FROM diary_entry WHERE title = '坏日期'").fetchone() is None


def test_new_entry_rejects_future_date(client):
    from datetime import date, timedelta

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    response = client.post(
        "/new", json={"title": "未来日期", "content": "x", "entry_date": tomorrow}
    )

    assert response.status_code == 400
    db = client.app.state.db
    assert db.execute("SELECT * FROM diary_entry WHERE title = '未来日期'").fetchone() is None
