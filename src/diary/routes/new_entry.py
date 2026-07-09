from datetime import datetime, timezone

from fastapi import APIRouter, Request
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
    now = datetime.now(timezone.utc).isoformat()
    safe_html = plain_text_to_safe_html(body["content"])
    cur = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES (?, ?, ?, ?, ?, 'manual')",
        (body["title"], body["content"], safe_html, body["content"], now),
    )
    return JSONResponse({"entry_id": cur.lastrowid})
