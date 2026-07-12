from datetime import datetime, timedelta, timezone

import unflincher.routes.new_entry as new_entry_module


FIXED_UTC_NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


def _freeze_utc_now(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return FIXED_UTC_NOW if tz is None else FIXED_UTC_NOW.astimezone(tz)

    monkeypatch.setattr(new_entry_module, "datetime", FrozenDateTime)


def test_new_entry_form_renders(client):
    response = client.get("/new")
    assert response.status_code == 200
    assert "New Entry" in response.text


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
