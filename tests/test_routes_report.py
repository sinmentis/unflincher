import diary.llm as llm_module


async def _fake_report_tokens(*args, **kwargs):
    for t in ["反复出现的主题：", "你总在", "岔路口犹豫"]:
        yield t


def test_report_page_shows_no_report_state(client):
    response = client.get("/report")
    assert response.status_code == 200
    assert "还没有生成过综合报告" in response.text


def test_generate_report_streams_and_persists_with_coverage(client, monkeypatch):
    monkeypatch.setattr(llm_module, "generate_report", _fake_report_tokens)
    db = client.app.state.db
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('早', '<p>a</p>', '<p>a</p>', 'a', '2020-01-01', 'import')"
    )
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('晚', '<p>b</p>', '<p>b</p>', 'b', '2026-01-01', 'import')"
    )

    response = client.post("/report/generate")

    assert response.status_code == 200
    assert "反复出现的主题" in response.text

    row = db.execute("SELECT * FROM aggregate_report WHERE status = 'ok'").fetchone()
    assert row["covered_entry_count"] == 2
    assert row["covered_from_date"] == "2020-01-01"
    assert row["covered_to_date"] == "2026-01-01"


def test_report_page_shows_current_report_after_generation(client, monkeypatch):
    monkeypatch.setattr(llm_module, "generate_report", _fake_report_tokens)
    db = client.app.state.db
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>a</p>', '<p>a</p>', 'a', '2026-01-01', 'import')"
    )
    client.post("/report/generate")

    response = client.get("/report")

    assert "反复出现的主题" in response.text
