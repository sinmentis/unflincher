from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from diary.sanitize import plain_text_to_safe_html

router = APIRouter()
templates = Jinja2Templates(directory="src/diary/templates")


@router.get("/new")
async def new_entry_form(request: Request):
    return templates.TemplateResponse(request, "new_entry.html", {})


@router.post("/new")
async def create_new_entry(request: Request, title: str = Form(...), content: str = Form(...)):
    db = request.app.state.db
    now = datetime.now(timezone.utc).isoformat()
    safe_html = plain_text_to_safe_html(content)
    cur = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES (?, ?, ?, ?, ?, 'manual')",
        (title, content, safe_html, content, now),
    )
    return RedirectResponse(url=f"/entry/{cur.lastrowid}", status_code=303)
