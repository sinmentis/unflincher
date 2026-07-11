"""Multi-session general chat — each chat_session row is an independent conversation over the
WHOLE diary corpus (thread_kind='general', session_id set), distinct from routes/entry.py's
per-entry chat (thread_kind='entry', entry_id set, session_id always NULL). Sessions are created
LAZILY: no chat_session row exists until the first real message of a new conversation is sent
(POST /chat/message, no session_id in the URL) — clicking "+ 新对话" alone never writes a row."""
import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from diary import llm
from diary.db import (
    create_chat_session,
    delete_chat_session,
    get_active_prompt,
    get_chat_session,
    list_chat_sessions,
    rename_chat_session,
    touch_chat_session,
)
from diary.sanitize import render_ai_markdown
from diary.templates_env import templates

router = APIRouter()

_TITLE_MODEL = "gpt-5.4-mini"


def _date_title() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _render_history(rows):
    return [
        {
            "role": m["role"],
            "content": m["content"],
            "content_html": render_ai_markdown(m["content"]) if m["role"] == "assistant" else None,
        }
        for m in rows
    ]


async def _generate_title_or_none(first_message: str) -> str | None:
    """Never lets a title-generation failure block the chat reply: returns None on any error, and
    the caller keeps the plain-date title the session was already created with."""
    try:
        summary = await llm.generate_session_title(first_message, _TITLE_MODEL)
    except Exception:
        return None
    return summary or None


@router.get("/chat")
async def chat_list(request: Request):
    db = request.app.state.db
    sessions = list_chat_sessions(db)
    return templates.TemplateResponse(request, "chat_list.html", {"sessions": sessions})


@router.get("/chat/new")
async def chat_new(request: Request):
    db = request.app.state.db
    sessions = list_chat_sessions(db)
    return templates.TemplateResponse(
        request, "chat_session.html",
        {"sessions": sessions, "active_session_id": None, "active_session_title": None, "history": []},
    )


@router.get("/chat/{session_id}")
async def chat_session_view(request: Request, session_id: int):
    db = request.app.state.db
    session = get_chat_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    sessions = list_chat_sessions(db)
    history_rows = db.execute(
        "SELECT role, content FROM chat_message WHERE session_id = ? ORDER BY id", (session_id,)
    ).fetchall()
    return templates.TemplateResponse(
        request, "chat_session.html",
        {
            "sessions": sessions,
            "active_session_id": session_id,
            "active_session_title": session["title"],
            "history": _render_history(history_rows),
        },
    )


@router.post("/chat/{session_id}/rename")
async def chat_rename(request: Request, session_id: int):
    db = request.app.state.db
    if get_chat_session(db, session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    body = await request.json()
    rename_chat_session(db, session_id, body["title"])
    return {"ok": True}


@router.post("/chat/{session_id}/delete")
async def chat_delete(request: Request, session_id: int):
    db = request.app.state.db
    if get_chat_session(db, session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    delete_chat_session(db, session_id)
    return {"ok": True}


@router.post("/chat/message")
async def send_new_session_message(request: Request):
    """Lazy-creation entry point. Creates the chat_session row up front (with a plain date title)
    so the user/assistant rows have somewhere to attach; kicks off title generation CONCURRENTLY
    with the streamed reply (the title only needs the first user message, so it has no
    dependency on the reply); reports the final session_id + title back in the `done` event's
    JSON payload, the same done-event-carries-data mechanism workshop's test-run preview uses."""
    db = request.app.state.db
    body = await request.json()
    user_message = body["message"]

    all_entries = db.execute("SELECT * FROM diary_entry ORDER BY entry_date").fetchall()
    active_prompt = get_active_prompt(db)
    model = active_prompt["model"]

    initial_title = _date_title()
    session_id = create_chat_session(db, initial_title)
    db.execute(
        "INSERT INTO chat_message (thread_kind, session_id, role, content) VALUES ('general', ?, 'user', ?)",
        (session_id, user_message),
    )

    title_task = asyncio.create_task(_generate_title_or_none(user_message))

    async def event_stream():
        chunks = []
        async for token in llm.general_chat_reply(
            [dict(e) for e in all_entries], [], user_message, active_prompt["body_text"], model,
        ):
            chunks.append(token)
            yield {"event": "token", "data": token}
        full_text = "".join(chunks)
        db.execute(
            "INSERT INTO chat_message (thread_kind, session_id, role, content, model) "
            "VALUES ('general', ?, 'assistant', ?, ?)",
            (session_id, full_text, model),
        )
        generated = await title_task
        final_title = initial_title
        if generated:
            final_title = f"{initial_title} · {generated}"
            rename_chat_session(db, session_id, final_title)
        touch_chat_session(db, session_id)
        yield {
            "event": "done",
            "data": json.dumps({"session_id": session_id, "title": final_title}, ensure_ascii=False),
        }

    return EventSourceResponse(event_stream(), sep="\n")


@router.post("/chat/{session_id}/message")
async def send_session_message(request: Request, session_id: int):
    db = request.app.state.db
    if get_chat_session(db, session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    body = await request.json()
    user_message = body["message"]

    all_entries = db.execute("SELECT * FROM diary_entry ORDER BY entry_date").fetchall()
    history_rows = db.execute(
        "SELECT role, content FROM chat_message WHERE session_id = ? ORDER BY id", (session_id,)
    ).fetchall()
    history = [{"role": r["role"], "content": r["content"]} for r in history_rows]

    db.execute(
        "INSERT INTO chat_message (thread_kind, session_id, role, content) VALUES ('general', ?, 'user', ?)",
        (session_id, user_message),
    )

    active_prompt = get_active_prompt(db)
    model = active_prompt["model"]

    async def event_stream():
        chunks = []
        async for token in llm.general_chat_reply(
            [dict(e) for e in all_entries], history, user_message, active_prompt["body_text"], model,
        ):
            chunks.append(token)
            yield {"event": "token", "data": token}
        full_text = "".join(chunks)
        db.execute(
            "INSERT INTO chat_message (thread_kind, session_id, role, content, model) "
            "VALUES ('general', ?, 'assistant', ?, ?)",
            (session_id, full_text, model),
        )
        touch_chat_session(db, session_id)
        yield {"event": "done", "data": "{}"}

    return EventSourceResponse(event_stream(), sep="\n")
