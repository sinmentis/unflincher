from datetime import date, datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from diary.sanitize import plain_text_to_safe_html

router = APIRouter()
templates = Jinja2Templates(directory="src/diary/templates")


@router.get("/new")
async def new_entry_form(request: Request):
    return templates.TemplateResponse(request, "new_entry.html", {})


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
        if picked_date > now.date():
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
