"""General chat — a single ongoing thread over the WHOLE corpus (thread_kind='general',
entry_id always NULL), distinct from routes/entry.py's per-entry chat. Both surfaces persist
into the same chat_message table but never share rows: the entry chat keys on entry_id, this
one keys on thread_kind='general' with entry_id left NULL."""
from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from diary import llm
from diary.config import load_settings
from diary.db import get_active_prompt

router = APIRouter()
templates = Jinja2Templates(directory="src/diary/templates")


@router.get("/chat")
async def chat_page(request: Request):
    db = request.app.state.db
    history = db.execute(
        "SELECT role, content FROM chat_message WHERE thread_kind='general' ORDER BY id"
    ).fetchall()
    return templates.TemplateResponse(request, "chat.html", {"history": history})


@router.post("/chat/message")
async def send_general_chat(request: Request):
    db = request.app.state.db
    body = await request.json()
    user_message = body["message"]

    all_entries = db.execute("SELECT * FROM diary_entry ORDER BY entry_date").fetchall()
    # Read the thread history BEFORE inserting the new user turn so the model sees prior
    # exchanges, not the message it is about to answer.
    history_rows = db.execute(
        "SELECT role, content FROM chat_message WHERE thread_kind='general' ORDER BY id"
    ).fetchall()
    history = [{"role": r["role"], "content": r["content"]} for r in history_rows]

    db.execute(
        "INSERT INTO chat_message (thread_kind, role, content) VALUES ('general', 'user', ?)",
        (user_message,),
    )

    active_prompt = get_active_prompt(db)
    settings = load_settings()

    async def event_stream():
        chunks = []
        async for token in llm.general_chat_reply(
            [dict(e) for e in all_entries],
            history,
            user_message,
            active_prompt["body_text"],
            settings.llm_model,
        ):
            chunks.append(token)
            yield {"event": "token", "data": token}
        full_text = "".join(chunks)
        db.execute(
            "INSERT INTO chat_message (thread_kind, role, content, model) "
            "VALUES ('general', 'assistant', ?, ?)",
            (full_text, settings.llm_model),
        )
        yield {"event": "done", "data": "{}"}

    return EventSourceResponse(event_stream(), sep="\n")
