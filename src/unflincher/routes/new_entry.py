from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from unflincher.db import get_distinct_entry_days
from unflincher.sanitize import plain_text_to_safe_html
from unflincher.templates_env import templates

router = APIRouter()


def _recency_context(db) -> dict:
    """Builds the New Entry page's quiet context line. This diary is used sporadically (not
    daily), so a naive streak counter would show 0 or 1 nearly always and read as broken. Only
    surface a streak once it reaches 2+ days; otherwise fall back to "last entry N days ago"
    (always meaningful regardless of cadence); a brand new diary with no entries yet shows
    nothing at all."""
    days = get_distinct_entry_days(db)
    if not days:
        return {}
    today = datetime.now(timezone.utc).date()
    day_set = {date.fromisoformat(d) for d in days}
    streak = 0
    cursor = today
    while cursor in day_set:
        streak += 1
        cursor -= timedelta(days=1)
    if streak >= 2:
        return {"streak_days": streak}
    days_since_last = (today - date.fromisoformat(days[0])).days
    return {"days_since_last": days_since_last}


@router.get("/new")
async def new_entry_form(request: Request):
    db = request.app.state.db
    return templates.TemplateResponse(request, "new_entry.html", _recency_context(db))


@router.post("/new")
async def create_new_entry(request: Request):
    db = request.app.state.db
    body = await request.json()
    now = datetime.now(timezone.utc)

    picked_date_str = body.get("entry_date")
    if picked_date_str:
        try:
            picked_date = date.fromisoformat(picked_date_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="entry_date must be YYYY-MM-DD")
        # The browser's date picker defaults/caps at LOCAL "today" (by design -- see the spec),
        # but this check runs against the server's UTC date. For any positive-UTC-offset
        # timezone (this app's owner is UTC+12), local "today" is genuinely one calendar day
        # ahead of UTC "today" for roughly half of every day -- rejecting that would reject the
        # picker's own default value. A one-day grace window keeps this a backstop against
        # clearly-bogus future dates (anything more than a day ahead) without fighting the
        # picker's legitimate default.
        if picked_date > now.date() + timedelta(days=1):
            raise HTTPException(status_code=400, detail="entry_date cannot be in the future")
        # Combine the picked DATE with the server's real current time-of-day, so entry_date
        # keeps the exact full-ISO-8601-with-offset format every other row already uses (a bare
        # YYYY-MM-DD would sort lexicographically BEFORE a same-day full timestamp, corrupting
        # same-day ordering relative to other rows -- see the plan's Global Constraints).
        entry_date = f"{picked_date.isoformat()}T{now.strftime('%H:%M:%S')}+00:00"
    else:
        # Backward compatibility: no entry_date field at all (e.g. a stale cached page from
        # before this change) behaves exactly as it always has.
        entry_date = now.isoformat()

    safe_html = plain_text_to_safe_html(body["content"])
    cur = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES (?, ?, ?, ?, ?, 'manual')",
        (body["title"], body["content"], safe_html, body["content"], entry_date),
    )
    return JSONResponse({"entry_id": cur.lastrowid})
