import unflincher.llm as llm_module


async def _fake_report_tokens(*args, **kwargs):
    for t in ["反复出现的主题：", "你总在", "岔路口犹豫"]:
        yield t


def test_report_page_shows_no_report_state(client):
    response = client.get("/report")
    assert response.status_code == 200
    assert "No report generated yet." in response.text


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


def test_view_specific_historical_report_version(client):
    db = client.app.state.db
    # is_active=0: the startup lifespan already seeds an active default persona and the
    # partial unique index allows only one is_active=1 row; this prompt is just a FK target.
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) VALUES (2, 'p', 'test-model', 0)"
    ).lastrowid
    old_id = db.execute(
        "INSERT INTO aggregate_report (prompt_version_id, model, body_text, covered_entry_count, "
        "status, created_at) VALUES (?, 'm', '半年前的报告', 5, 'ok', '2026-01-01T00:00:00')",
        (prompt_id,),
    ).lastrowid

    response = client.get(f"/report/{old_id}")

    assert response.status_code == 200
    assert "半年前的报告" in response.text


def test_report_page_shows_sidebar_timeline_with_active_and_failed_states(client):
    db = client.app.state.db
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) VALUES (2, 'p', 'm', 0)"
    ).lastrowid
    old_failed_id = db.execute(
        "INSERT INTO aggregate_report (prompt_version_id, model, body_text, covered_entry_count, "
        "status, created_at) VALUES (?, 'm', '', 0, 'failed', '2026-06-28T21:40:00')",
        (prompt_id,),
    ).lastrowid
    current_id = db.execute(
        "INSERT INTO aggregate_report (prompt_version_id, model, body_text, covered_entry_count, "
        "status, created_at) VALUES (?, 'm', '当前报告', 82, 'ok', '2026-07-10T14:20:00')",
        (prompt_id,),
    ).lastrowid

    response = client.get(f"/report/{current_id}")

    assert response.status_code == 200
    assert 'class="side-nav side-nav--report-versions"' in response.text
    assert "Failed" in response.text
    assert "82" in response.text
    # the currently-viewed version's node carries the active-state class, matching the
    # year-filter sidebar's .side-nav-item.active convention.
    assert f'href="/report/{current_id}" class="side-nav-item active"' in response.text
    assert f'href="/report/{old_failed_id}"' in response.text
