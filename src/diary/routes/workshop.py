from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from diary import llm
from diary.config import load_settings
from diary.db import get_active_prompt, set_active_prompt

router = APIRouter()
templates = Jinja2Templates(directory="src/diary/templates")


@router.get("/workshop")
async def workshop_page(request: Request):
    db = request.app.state.db
    active_prompt = get_active_prompt(db)
    entries = db.execute("SELECT id, title FROM diary_entry ORDER BY entry_date").fetchall()
    return templates.TemplateResponse(
        request,
        "workshop.html",
        {
            "active_prompt": active_prompt["body_text"] if active_prompt else "",
            "entries": entries,
        },
    )


@router.post("/workshop/test-run")
async def workshop_test_run(request: Request):
    """Preview only — NEVER writes to the database, not even a log line with the draft text.

    Runs the exact same llm.generate_commentary function, with the exact same full-corpus
    context, as the real per-entry trigger (Task 9). Only then does the preview faithfully
    predict the real output. The single picked entry is the focus, but the model still sees
    every entry for cross-entry pattern matching — passing just the one entry here would make
    the preview lie about what the real generation produces.
    """
    db = request.app.state.db
    body = await request.json()
    entry = db.execute("SELECT * FROM diary_entry WHERE id = ?", (body["entry_id"],)).fetchone()
    all_entries = db.execute("SELECT * FROM diary_entry ORDER BY entry_date").fetchall()
    settings = load_settings()

    async def event_stream():
        async for token in llm.generate_commentary(
            dict(entry),
            [dict(e) for e in all_entries],
            body["draft_prompt"],
            settings.llm_model,
        ):
            yield {"event": "token", "data": token}
        yield {"event": "done", "data": "{}"}

    return EventSourceResponse(event_stream(), sep="\n")


@router.post("/workshop/apply")
async def workshop_apply(request: Request):
    """Commit the draft as the new active persona version. No generation happens here — that is
    Task 16's apply-to-all path. This is purely a version swap."""
    db = request.app.state.db
    body = await request.json()
    new_id = set_active_prompt(db, body["draft_prompt"])
    return {"persona_prompt_id": new_id}
