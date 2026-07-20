import pytest

import unflincher.llm as llm_module


@pytest.fixture(autouse=True)
def _fake_model_limit(monkeypatch):
    """Report generation now preflights against get_model_max_prompt_tokens() before opening
    the SSE stream -- fake it so tests never need a real Copilot client just to pass preflight."""
    async def _fake_limit(model):
        return 200_000
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _fake_limit)


async def _fake_report_tokens(envelope):
    for t in ["反复出现的主题：", "你总在", "岔路口犹豫"]:
        yield t


def test_report_page_shows_no_report_state(client):
    response = client.get("/report")
    assert response.status_code == 200
    assert "No report generated yet." in response.text


def test_report_page_uses_balanced_report_structure(client):
    body = client.get("/report").text
    assert 'class="report-layout"' in body
    assert 'data-role="primary-task"' in body
    assert 'data-role="report-body"' in body
    assert 'data-role="report-history"' in body
    assert 'class="report-version-index"' in body
    assert 'id="report-toc"' in body
    assert 'id="report-stream"' in body
    assert 'id="run-report"' in body
    assert 'src="/static/js/report.js"' in body


def test_failed_historical_report_renders_failed_state(client):
    db = client.app.state.db
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) "
        "VALUES (2, 'p', 'test-model', 0)"
    ).lastrowid
    failed_id = db.execute(
        "INSERT INTO aggregate_report "
        "(prompt_version_id, model, body_text, covered_entry_count, status, error) "
        "VALUES (?, 'test-model', '', 0, 'failed', 'boom')",
        (prompt_id,),
    ).lastrowid
    body = client.get(f"/report/{failed_id}").text
    assert 'data-report-status="failed"' in body
    assert "Failed" in body
    assert "boom" in body


def test_report_markdown_headings_stay_below_the_page_heading(client):
    db = client.app.state.db
    prompt_id = db.execute(
        "SELECT id FROM persona_prompt WHERE is_active = 1"
    ).fetchone()["id"]
    db.execute(
        "INSERT INTO aggregate_report "
        "(prompt_version_id, model, body_text, covered_entry_count, status) "
        "VALUES (?, 'test-model', '# Investigation\n\n## Pattern\n\n### Detail', 0, 'ok')",
        (prompt_id,),
    )

    body = client.get("/report").text

    assert 'id="report-body"' in body
    assert body.count("<h1") == 1
    assert "<h2>Investigation</h2>" in body
    assert "<h3>Pattern</h3>" in body
    assert "<h4>Detail</h4>" in body


def test_generate_report_streams_and_persists_with_coverage(client, monkeypatch):
    monkeypatch.setattr(llm_module, "stream_completion_envelope", _fake_report_tokens)
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
    monkeypatch.setattr(llm_module, "stream_completion_envelope", _fake_report_tokens)
    db = client.app.state.db
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>a</p>', '<p>a</p>', 'a', '2026-01-01', 'import')"
    )
    client.post("/report/generate")

    response = client.get("/report")

    assert "反复出现的主题" in response.text


def test_generate_report_releases_lease_after_stream_completes(client, monkeypatch):
    from unflincher.db import get_lease_by_target, report_target_key

    monkeypatch.setattr(llm_module, "stream_completion_envelope", _fake_report_tokens)
    client.post("/report/generate")

    assert get_lease_by_target(client.app.state.db, report_target_key()) is None


def test_generate_report_409_when_target_already_leased(client):
    from unflincher.db import acquire_lease, report_target_key

    db = client.app.state.db
    acquire_lease(db, report_target_key(), "background", "someone-else")

    response = client.post("/report/generate")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "target_busy"
    assert db.execute("SELECT COUNT(*) AS n FROM aggregate_report").fetchone()["n"] == 0


def test_generate_report_413_releases_lease_and_writes_nothing(client, monkeypatch):
    from unflincher.db import get_lease_by_target, report_target_key

    async def _tiny_limit(model):
        return 1
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _tiny_limit)

    db = client.app.state.db
    response = client.post("/report/generate")

    assert response.status_code == 413
    assert response.json()["detail"]["reason"] == "context_too_large"
    assert db.execute("SELECT COUNT(*) AS n FROM aggregate_report").fetchone()["n"] == 0
    assert get_lease_by_target(db, report_target_key()) is None


def test_report_coverage_dates_render_as_calendar_dates_not_timestamps(client):
    """Regression: coverage metadata must slice entry timestamps to YYYY-MM-DD, matching the
    [:10] date convention used across timeline/entry/version templates. Full ISO timestamps
    (with H:M:S) leak seconds-level noise and wrap the coverage line onto two rows on mobile."""
    db = client.app.state.db
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) VALUES (2, 'p', 'm', 0)"
    ).lastrowid
    db.execute(
        "INSERT INTO aggregate_report (prompt_version_id, model, body_text, covered_entry_count, "
        "covered_from_date, covered_to_date, status) "
        "VALUES (?, 'm', '主题', 79, '2014-10-01 20:57:17', '2026-01-08 06:46:57', 'ok')",
        (prompt_id,),
    )

    body = client.get("/report").text

    assert "2014-10-01 — 2026-01-08" in body
    assert "20:57:17" not in body
    assert "06:46:57" not in body


def test_report_coverage_shows_how_many_archive_entries_are_not_covered(client):
    db = client.app.state.db
    prompt_id = db.execute(
        "SELECT id FROM persona_prompt WHERE is_active = 1"
    ).fetchone()["id"]
    for index, entry_date in enumerate(("2026-01-01", "2026-02-01", "2026-03-01"), start=1):
        db.execute(
            "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
            "entry_date, source) VALUES (?, '<p>entry</p>', '<p>entry</p>', 'entry', ?, 'manual')",
            (f"Entry {index}", entry_date),
        )
    report_id = db.execute(
        "INSERT INTO aggregate_report (prompt_version_id, model, body_text, covered_entry_count, "
        "covered_from_date, covered_to_date, status) "
        "VALUES (?, 'm', 'Report body', 2, '2026-01-01', '2026-02-01', 'ok')",
        (prompt_id,),
    ).lastrowid

    for path in ("/report", f"/report/{report_id}"):
        body = client.get(path).text
        assert "Covers 2 entries · 2026-01-01 — 2026-02-01 · 1 behind" in body


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
    assert 'class="report-version-index"' in response.text
    assert "Failed" in response.text
    assert "82" in response.text
    assert f'href="/report/{current_id}"' in response.text
    assert 'aria-current="true"' in response.text
    assert f'href="/report/{old_failed_id}"' in response.text
    assert 'data-status="failed"' in response.text


def test_generate_report_uses_canonical_archive_order_for_same_date_entries(client, monkeypatch):
    captured = {}

    async def fake_stream(envelope):
        captured["user_content"] = envelope.user_content
        yield "ok"

    monkeypatch.setattr(llm_module, "stream_completion_envelope", fake_stream)
    db = client.app.state.db
    # Two entries sharing the SAME entry_date, inserted in REVERSE of their intended (entry_date,
    # id) order -- id must be the deciding tiebreaker.
    later_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('B在后', '<p>b</p>', '<p>b</p>', 'b', '2026-03-01', 'import')"
    ).lastrowid
    earlier_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('A在前', '<p>a</p>', '<p>a</p>', 'a', '2026-03-01', 'import')"
    ).lastrowid
    assert earlier_id > later_id  # sanity: id order is the OPPOSITE of the desired output order

    client.post("/report/generate")

    assert captured["user_content"].index("B在后") < captured["user_content"].index("A在前")


# ---------------------------------------------------------------------------
# Perspective indicator (Task: perspective indicators). Life Report shows "Perspective: <name>"
# for whichever report version is displayed (current or historical) using that version's joined
# prompt-version preset_key.
# ---------------------------------------------------------------------------

def test_report_page_has_no_perspective_indicator_when_no_report_exists(client):
    body = client.get("/report").text
    assert 'data-role="perspective-indicator"' not in body


def test_report_page_shows_perspective_for_current_report(client):
    db = client.app.state.db
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, preset_key, is_active) "
        "VALUES (2, 'p', 'test-model', 'analyst', 0)"
    ).lastrowid
    db.execute(
        "INSERT INTO aggregate_report "
        "(prompt_version_id, model, body_text, covered_entry_count, status) "
        "VALUES (?, 'test-model', 'body', 1, 'ok')",
        (prompt_id,),
    )

    body = client.get("/report").text

    assert "Perspective: Analyst" in body


def test_report_page_shows_custom_for_null_preset_key(client):
    db = client.app.state.db
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) "
        "VALUES (2, 'p', 'test-model', 0)"
    ).lastrowid
    db.execute(
        "INSERT INTO aggregate_report "
        "(prompt_version_id, model, body_text, covered_entry_count, status) "
        "VALUES (?, 'test-model', 'body', 1, 'ok')",
        (prompt_id,),
    )

    body = client.get("/report").text

    assert "Perspective: Custom" in body


def test_view_historical_report_version_shows_its_own_perspective(client):
    db = client.app.state.db
    old_prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, preset_key, is_active) "
        "VALUES (2, 'old', 'test-model', 'coach', 0)"
    ).lastrowid
    new_prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, preset_key, is_active) "
        "VALUES (3, 'new', 'test-model', 'challenger', 0)"
    ).lastrowid
    old_id = db.execute(
        "INSERT INTO aggregate_report "
        "(prompt_version_id, model, body_text, covered_entry_count, status, created_at) "
        "VALUES (?, 'test-model', 'old body', 1, 'ok', '2026-01-01T00:00:00')",
        (old_prompt_id,),
    ).lastrowid
    db.execute(
        "INSERT INTO aggregate_report "
        "(prompt_version_id, model, body_text, covered_entry_count, status, created_at) "
        "VALUES (?, 'test-model', 'new body', 1, 'ok', '2026-01-02T00:00:00')",
        (new_prompt_id,),
    )

    body = client.get(f"/report/{old_id}").text

    assert "Perspective: Coach" in body
    assert "Perspective: Challenger" not in body


def test_report_page_shows_custom_for_unknown_historical_preset_key(client):
    db = client.app.state.db
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, preset_key, is_active) "
        "VALUES (2, 'p', 'test-model', 'retired-preset', 0)"
    ).lastrowid
    db.execute(
        "INSERT INTO aggregate_report "
        "(prompt_version_id, model, body_text, covered_entry_count, status) "
        "VALUES (?, 'test-model', 'body', 1, 'ok')",
        (prompt_id,),
    )

    body = client.get("/report").text

    assert "Perspective: Custom" in body


# ---------------------------------------------------------------------------
# Life Report first-generation action (workstream 7). Same button id/class, same route -- only
# its wording changes, based on whether a Life Report has EVER succeeded (status='ok'), matching
# the "current" == latest ok row rule used everywhere else (see get_current_report).
# ---------------------------------------------------------------------------

def test_report_empty_state_shows_first_report_wording(client):
    body = client.get("/report").text
    assert 'id="run-report"' in body
    assert "Generate the first report" in body
    assert "Generate / regenerate report" not in body


def test_report_shows_regenerate_wording_once_a_report_has_succeeded(client, monkeypatch):
    monkeypatch.setattr(llm_module, "stream_completion_envelope", _fake_report_tokens)
    db = client.app.state.db
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>a</p>', '<p>a</p>', 'a', '2026-01-01', 'import')"
    )
    client.post("/report/generate")

    body = client.get("/report").text

    assert "Generate / regenerate report" in body
    assert "Generate the first report" not in body


def test_report_keeps_first_report_wording_when_only_a_failed_report_exists(client):
    db = client.app.state.db
    prompt_id = db.execute(
        "SELECT id FROM persona_prompt WHERE is_active = 1"
    ).fetchone()["id"]
    db.execute(
        "INSERT INTO aggregate_report (prompt_version_id, model, body_text, "
        "covered_entry_count, status, error) VALUES (?, 'm', '', 0, 'failed', 'boom')",
        (prompt_id,),
    )

    body = client.get("/report").text

    assert "Generate the first report" in body
    assert "Generate / regenerate report" not in body


def test_viewing_an_old_failed_version_still_shows_regenerate_once_any_report_succeeded(client, monkeypatch):
    """A specific historical version being viewed may itself be 'failed', but the button
    wording reflects the OVERALL Life Report state (any successful report ever), not the
    one version currently on screen."""
    monkeypatch.setattr(llm_module, "stream_completion_envelope", _fake_report_tokens)
    db = client.app.state.db
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>a</p>', '<p>a</p>', 'a', '2026-01-01', 'import')"
    )
    client.post("/report/generate")
    prompt_id = db.execute(
        "SELECT id FROM persona_prompt WHERE is_active = 1"
    ).fetchone()["id"]
    failed_id = db.execute(
        "INSERT INTO aggregate_report (prompt_version_id, model, body_text, "
        "covered_entry_count, status, error) VALUES (?, 'm', '', 0, 'failed', 'boom')",
        (prompt_id,),
    ).lastrowid

    body = client.get(f"/report/{failed_id}").text

    assert "Generate / regenerate report" in body
    assert "Generate the first report" not in body


def test_first_report_action_still_posts_to_the_existing_generate_route(client):
    """No second generation path is introduced -- the wording-only change must still submit to
    the same /report/generate route the JS already targets."""
    import re
    from pathlib import Path

    js = Path("src/unflincher/static/js/report.js").read_text()
    assert '"/report/generate"' in js
    assert re.search(r'id="run-report"', client.get("/report").text)
