from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates

from diary.db import get_current_commentary
from diary.sanitize import render_ai_markdown

router = APIRouter()
templates = Jinja2Templates(directory="src/diary/templates")


@router.get("/entry/{entry_id}")
async def entry_detail(request: Request, entry_id: int):
    db = request.app.state.db
    entry = db.execute("SELECT * FROM diary_entry WHERE id = ?", (entry_id,)).fetchone()
    if entry is None:
        raise HTTPException(status_code=404, detail="entry not found")

    commentary = get_current_commentary(db, entry_id)
    commentary_html = render_ai_markdown(commentary["body_text"]) if commentary else None

    return templates.TemplateResponse(
        request, "entry_detail.html", {"entry": entry, "commentary_html": commentary_html}
    )
