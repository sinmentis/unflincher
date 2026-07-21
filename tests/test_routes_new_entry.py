import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import unflincher.routes.new_entry as new_entry_module


FIXED_UTC_NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)

ROOT = Path(__file__).resolve().parents[1]
PAGES_CSS = ROOT / "src" / "unflincher" / "static" / "css" / "pages.css"


def _freeze_utc_now(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return FIXED_UTC_NOW if tz is None else FIXED_UTC_NOW.astimezone(tz)

    monkeypatch.setattr(new_entry_module, "datetime", FrozenDateTime)


def test_new_entry_form_renders(client):
    response = client.get("/new")
    assert response.status_code == 200
    assert "Write" in response.text


def test_new_entry_css_does_not_override_field_error_color():
    # Regression (Task 8): the shared, accessible `.field-error { color: var(--text) }`
    # in components.css must win. The Write page CSS previously redefined
    # `.field-error` to `var(--accent)`, which aliases `--muted` (#85827b) and fails
    # 4.5:1 contrast on /new. The page-specific stylesheet must not re-color it.
    css = PAGES_CSS.read_text()
    for match in re.finditer(r"\.field-error\s*\{([^}]*)\}", css):
        block = match.group(1)
        assert "var(--accent)" not in block, (
            "pages.css must not override .field-error color to --accent"
        )
        assert "var(--muted)" not in block, (
            "pages.css must not override .field-error color to --muted"
        )


def test_new_entry_page_uses_balanced_writing_desk(client):
    body = client.get("/new").text
    assert 'class="writing-desk-frame"' in body
    assert 'class="writing-desk"' in body
    assert 'data-role="primary-task"' in body
    assert 'data-role="entry-metadata"' in body
    assert 'data-role="entry-editor"' in body
    assert 'aria-labelledby="new-entry-heading"' in body
    assert 'id="draft-status"' in body
    assert 'aria-live="polite"' in body
    assert 'id="new-entry-notice"' in body
    assert 'id="new-date-error"' in body
    assert 'src="/static/js/new-entry.js"' in body
    # Exactly one save action: the quiet metadata/editor split never introduces a
    # second trigger such as a mock-only "Save and generate commentary" button.
    assert body.count('type="submit"') == 1
    assert "style=" not in body
    # Source order matches the approved mock: the quiet metadata rail precedes the
    # dominant editor, and the responsive layout keeps that order without CSS `order`.
    assert body.index('data-role="entry-metadata"') < body.index('data-role="entry-editor"')
    # Native constraint validation is disabled so a future date still reaches the
    # JS handler and server-400 path, which show the localized field/form errors
    # instead of a native browser bubble (Task 8 review fix).
    form_open_tag = re.search(r'<form\b[^>]*\bid="new-entry-form"[^>]*>', body).group(0)
    assert "novalidate" in form_open_tag


def test_new_entry_page_has_day_of_week_and_live_word_count_scaffolding(client):
    body = client.get("/new").text
    assert 'id="entry-day-of-week"' in body
    assert 'id="entry-word-count"' in body
    form_open_tag = re.search(r'<form\b[^>]*\bid="new-entry-form"[^>]*>', body).group(0)
    # The raw "{count} words" template is left un-interpolated here -- new-entry.js
    # substitutes {count} client-side on every keystroke (see updateWordCount).
    assert 'data-word-count-label="{count} words"' in form_open_tag


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


def _seed_diary_entry(db, entry_date: str, title: str = "e"):
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES (?, '<p>x</p>', '<p>x</p>', 'x', ?, 'import')",
        (title, entry_date),
    )


def test_new_entry_shows_no_recency_note_for_a_fresh_archive(client):
    body = client.get("/new").text
    assert 'class="entry-recency-note"' not in body


def test_new_entry_shows_days_since_last_when_streak_is_short(client, monkeypatch):
    _freeze_utc_now(monkeypatch)  # "today" = 2026-07-12
    db = client.app.state.db
    _seed_diary_entry(db, "2026-07-05T09:00:00")

    body = client.get("/new").text

    assert "Last entry 7 days ago" in body
    assert "writing streak" not in body


def test_new_entry_shows_streak_for_consecutive_days_including_today(client, monkeypatch):
    _freeze_utc_now(monkeypatch)  # "today" = 2026-07-12
    db = client.app.state.db
    for day, title in [("2026-07-10", "a"), ("2026-07-11", "b"), ("2026-07-12", "c")]:
        _seed_diary_entry(db, f"{day}T08:00:00", title=title)

    body = client.get("/new").text

    assert "3-day writing streak" in body
    assert "Last entry" not in body


def test_new_entry_single_day_streak_falls_back_to_days_since_last(client, monkeypatch):
    # A streak of exactly 1 (only today, no run of consecutive days) is not surfaced as a
    # "streak" -- it reads as days-since-last with a count of 0, matching _recency_context's
    # documented `streak >= 2` threshold.
    _freeze_utc_now(monkeypatch)  # "today" = 2026-07-12
    db = client.app.state.db
    _seed_diary_entry(db, "2026-07-12T08:00:00")

    body = client.get("/new").text

    assert "Last entry 0 days ago" in body
    assert "writing streak" not in body


def test_new_entry_rejects_malformed_date(client):
    response = client.post(
        "/new", json={"title": "坏日期", "content": "x", "entry_date": "not-a-date"}
    )

    assert response.status_code == 400
    db = client.app.state.db
    assert db.execute("SELECT * FROM diary_entry WHERE title = '坏日期'").fetchone() is None


def test_new_entry_rejects_future_date(client, monkeypatch):
    # More than one day ahead of the server's UTC "today" must still be rejected -- the grace
    # window below (see test_new_entry_accepts_tomorrow_for_positive_utc_offset_timezones) only
    # covers exactly one day ahead, not arbitrary future dates.
    _freeze_utc_now(monkeypatch)

    day_after_tomorrow = (FIXED_UTC_NOW.date() + timedelta(days=2)).isoformat()
    response = client.post(
        "/new", json={"title": "未来日期", "content": "x", "entry_date": day_after_tomorrow}
    )

    assert response.status_code == 400
    db = client.app.state.db
    assert db.execute("SELECT * FROM diary_entry WHERE title = '未来日期'").fetchone() is None


def test_new_entry_accepts_tomorrow_for_positive_utc_offset_timezones(client, monkeypatch):
    # Regression guard: the browser's date picker defaults/caps at LOCAL "today", but this
    # server validates against its own UTC date. For any positive-UTC-offset timezone (this
    # app's owner is UTC+12), local "today" is genuinely one calendar day ahead of UTC "today"
    # for roughly half of every day -- without a one-day grace window, picking the picker's own
    # default value would be rejected as "in the future" during that window.
    _freeze_utc_now(monkeypatch)

    tomorrow = (FIXED_UTC_NOW.date() + timedelta(days=1)).isoformat()
    response = client.post(
        "/new", json={"title": "本地明天", "content": "x", "entry_date": tomorrow}
    )

    assert response.status_code == 200
    db = client.app.state.db
    row = db.execute("SELECT entry_date FROM diary_entry WHERE title = '本地明天'").fetchone()
    assert row is not None and row["entry_date"].startswith(tomorrow)
