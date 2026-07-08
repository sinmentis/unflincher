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
    chat_history = db.execute(
        "SELECT role, content FROM chat_message WHERE thread_kind='entry' AND entry_id=? ORDER BY id",
        (entry_id,),
    ).fetchall()

    return templates.TemplateResponse(
        request,
        "entry_detail.html",
        {"entry": entry, "commentary_html": commentary_html, "chat_history": chat_history},
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


@router.post("/entry/{entry_id}/chat")
async def entry_chat(request: Request, entry_id: int):
    db = request.app.state.db
    entry = db.execute("SELECT * FROM diary_entry WHERE id = ?", (entry_id,)).fetchone()
    if entry is None:
        raise HTTPException(status_code=404, detail="entry not found")
    body = await request.json()
    user_message = body["message"]

    # The thread is keyed by entry_id alone, so history spans every past exchange regardless
    # of which entry_commentary version was current at the time. Read it before inserting the
    # new user turn so the model sees prior turns, not the message it is about to answer.
    history_rows = db.execute(
        "SELECT role, content FROM chat_message WHERE thread_kind='entry' AND entry_id=? ORDER BY id",
        (entry_id,),
    ).fetchall()
    history = [{"role": r["role"], "content": r["content"]} for r in history_rows]

    db.execute(
        "INSERT INTO chat_message (thread_kind, entry_id, role, content) VALUES ('entry', ?, 'user', ?)",
        (entry_id, user_message),
    )

    # Always ground the reply in the LATEST status='ok' commentary, never a specific/viewed
    # version — get_current_commentary already returns the newest ok row.
    commentary = get_current_commentary(db, entry_id)
    commentary_text = commentary["body_text"] if commentary else None
    active_prompt = get_active_prompt(db)
    settings = load_settings()

    async def event_stream():
        chunks = []
        async for token in llm.chat_reply(
            dict(entry),
            commentary_text,
            history,
            user_message,
            active_prompt["body_text"],
            settings.llm_model,
        ):
            chunks.append(token)
            yield {"event": "token", "data": token}
        full_text = "".join(chunks)
        db.execute(
            "INSERT INTO chat_message (thread_kind, entry_id, role, content, model) "
            "VALUES ('entry', ?, 'assistant', ?, ?)",
            (entry_id, full_text, settings.llm_model),
        )
        yield {"event": "done", "data": "{}"}

    return EventSourceResponse(event_stream(), sep="\n")
