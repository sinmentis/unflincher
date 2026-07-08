from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from diary import llm
from diary.config import load_settings
from diary.db import get_active_prompt, get_current_commentary
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


@router.post("/entry/{entry_id}/commentary")
async def trigger_entry_commentary(request: Request, entry_id: int):
    db = request.app.state.db
    entry = db.execute("SELECT * FROM diary_entry WHERE id = ?", (entry_id,)).fetchone()
    if entry is None:
        raise HTTPException(status_code=404, detail="entry not found")
    all_entries = db.execute("SELECT * FROM diary_entry ORDER BY entry_date").fetchall()
    active_prompt = get_active_prompt(db)
    settings = load_settings()

    async def event_stream():
        chunks = []
        async for token in llm.generate_commentary(
            dict(entry), [dict(e) for e in all_entries], active_prompt["body_text"], settings.llm_model
        ):
            chunks.append(token)
            yield {"event": "token", "data": token}
        full_text = "".join(chunks)
        db.execute(
            "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status) "
            "VALUES (?, ?, ?, ?, 'ok')",
            (entry_id, active_prompt["id"], settings.llm_model, full_text),
        )
        yield {"event": "done", "data": "{}"}

    return EventSourceResponse(event_stream(), sep="\n")
