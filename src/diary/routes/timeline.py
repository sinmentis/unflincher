from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from diary.db import get_current_commentary

router = APIRouter()
templates = Jinja2Templates(directory="src/diary/templates")


@router.get("/")
async def timeline(request: Request):
    db = request.app.state.db
    rows = db.execute(
        "SELECT id, title, entry_date FROM diary_entry ORDER BY entry_date DESC"
    ).fetchall()
    entries = []
    for row in rows:
        commentary = get_current_commentary(db, row["id"])
        entries.append({
            "id": row["id"], "title": row["title"], "entry_date": row["entry_date"],
            "has_commentary": commentary is not None,
        })
    return templates.TemplateResponse(request, "timeline.html", {"entries": entries})
