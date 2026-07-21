"""Multi-session general chat — each chat_session row is an independent conversation over the
WHOLE diary corpus (thread_kind='general', session_id set), distinct from routes/entry.py's
per-entry chat (thread_kind='entry', entry_id set, session_id always NULL). Sessions are created
LAZILY: no chat_session row exists until the first real message of a new conversation is sent
(POST /chat/message, no session_id in the URL) — clicking "+ 新对话" alone never writes a row.

Every turn (new or existing thread) acquires a lease BEFORE reading history or writing any
message -- see db.entry_thread_key/conversation_thread_key/acquire_lease -- and releases it once
the stream ends, success or failure. A new general Conversation uses a temporary request-scoped
lease during preflight, then converts that SAME lease to conversation:<session_id> in one atomic
transaction with the session/message creation (see
db.create_general_chat_session_and_convert_lease) -- never a separate convert_lease_target() call."""
import asyncio
import contextlib
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from unflincher import llm
from unflincher.context_budget import ContextTooLargeError, ModelLimitsUnavailableError
from unflincher.db import (
    MaintenanceLockedError,
    RequestLeaseExpiredError,
    TargetBusyError,
    acquire_lease,
    conversation_thread_key,
    create_general_chat_session_and_convert_lease,
    delete_chat_session,
    get_active_prompt,
    get_chat_session,
    get_entries_in_order,
    get_ordered_entry_ids,
    list_chat_sessions,
    new_request_lease_key,
    release_lease,
    rename_chat_session,
    touch_chat_session,
)
from unflincher.i18n import t
from unflincher.perspectives import display_name_key
from unflincher.routes.errors import generation_safety_http_exception
from unflincher.routes.sse import sse_response
from unflincher.sanitize import render_ai_markdown
from unflincher.templates_env import get_current_language, templates

logger = logging.getLogger(__name__)

router = APIRouter()

_TITLE_MODEL = "gpt-5.4-mini"


def _date_title() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _next_response_perspective_name(request: Request, db) -> str:
    """The Perspective that will answer the NEXT turn on this thread -- always the globally
    active prompt's, never a per-thread/per-message one (see plan: "Perspective for the next
    response" applies to future generation only)."""
    active_prompt = get_active_prompt(db)
    preset_key = active_prompt["preset_key"] if active_prompt else None
    return t(get_current_language(request), display_name_key(preset_key))


def _render_history(rows):
    return [
        {
            "role": m["role"],
            "content": m["content"],
            "content_html": render_ai_markdown(m["content"]) if m["role"] == "assistant" else None,
            "created_at": m["created_at"],
        }
        for m in rows
    ]


async def _generate_title_or_none(db, owner_token: str, first_message: str) -> str | None:
    """Never lets title generation block or fail the main conversation. Acquires its OWN
    temporary request lease (see db.new_request_lease_key) BEFORE preparing/preflighting --
    distinct from the main conversation's lease, so the deploy drain can observe this as separate
    admitted work -- and releases it in `finally` regardless of outcome. Independently prepares
    and preflights the title request against its OWN fixed model (_TITLE_MODEL): if maintenance
    is locked, that model's limit is unavailable, or the request is too large, title generation is
    explicitly skipped (keeping the date title) and the reason is logged, rather than silently
    guessing a limit or letting a title failure affect the validated main conversation reply."""
    try:
        lease_id = acquire_lease(db, new_request_lease_key(), "request", owner_token)
    except MaintenanceLockedError as exc:
        logger.info("session title generation skipped (%s): %s", type(exc).__name__, exc)
        return None

    try:
        try:
            prepared = await llm.prepare_title_request(first_message, _TITLE_MODEL)
        except (ContextTooLargeError, ModelLimitsUnavailableError) as exc:
            logger.info(
                "session title generation skipped (%s): %s", type(exc).__name__, exc,
            )
            return None
        try:
            chunks = [t async for t in llm.generate_from_prepared(prepared)]
        except Exception:
            logger.info("session title generation failed after preflight", exc_info=True)
            return None
        text = "".join(chunks).strip()
        return text or None
    finally:
        release_lease(db, lease_id)


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
        {
            "sessions": sessions, "active_session_id": None, "active_session_title": None,
            "history": [], "next_response_perspective_name": _next_response_perspective_name(request, db),
        },
    )


@router.get("/chat/{session_id}")
async def chat_session_view(request: Request, session_id: int):
    db = request.app.state.db
    session = get_chat_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    sessions = list_chat_sessions(db)
    history_rows = db.execute(
        "SELECT role, content, created_at FROM chat_message WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    return templates.TemplateResponse(
        request, "chat_session.html",
        {
            "sessions": sessions,
            "active_session_id": session_id,
            "active_session_title": session["title"],
            "history": _render_history(history_rows),
            "next_response_perspective_name": _next_response_perspective_name(request, db),
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
    """delete_chat_session() ACQUIRES this session's thread lease (in its own delete transaction)
    before deleting -- a busy session (an active turn already holds the lease) returns a no-write
    409 and is preserved, never deleted out from under an in-flight stream."""
    db = request.app.state.db
    if get_chat_session(db, session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        delete_chat_session(db, session_id, request.app.state.owner_token)
    except TargetBusyError as exc:
        raise generation_safety_http_exception(exc) from exc
    return {"ok": True}


@router.post("/chat/message")
async def send_new_session_message(request: Request):
    """Lazy-creation entry point. Acquires a temporary request-scoped lease (see
    db.new_request_lease_key) BEFORE preparing or preflighting anything (the ONLY permitted
    preflight write for a brand-new thread); only after preflight succeeds does
    db.create_general_chat_session_and_convert_lease() atomically create the session, insert the
    first user message, and convert that SAME lease to conversation:<session_id> -- durable
    writes never happen before validation. Kicks off title generation CONCURRENTLY with the
    streamed reply, under its OWN separate request lease (see _generate_title_or_none) -- the
    title only needs the first user message, so it has no dependency on the reply; reports the
    final session_id + title back in the `done` event's JSON payload, the same
    done-event-carries-data mechanism workshop's test-run preview uses.

    The main conversation lease is held until the title task has FULLY settled: the shared
    _cleanup below (see routes/sse.py's sse_response) cancels and awaits the title task first
    (letting its own lease-release run) before releasing the main lease -- the title task can
    never become an orphan holding its own lease past the end of this request, whether the main
    stream completes normally, fails partway, or the client disconnects even before the SSE body
    ever starts iterating."""
    db = request.app.state.db
    body = await request.json()
    user_message = body["message"]
    owner_token = request.app.state.owner_token

    try:
        lease_id = acquire_lease(db, new_request_lease_key(), "request", owner_token)
    except MaintenanceLockedError as exc:
        raise generation_safety_http_exception(exc) from exc

    try:
        preflight_entry_ids = get_ordered_entry_ids(db)
        all_entries = [dict(row) for row in get_entries_in_order(db, preflight_entry_ids)]
        active_prompt = get_active_prompt(db)
        model = active_prompt["model"]
        try:
            prepared = await llm.prepare_general_chat_request(
                all_entries, [], user_message, active_prompt["body_text"], model,
            )
        except (ContextTooLargeError, ModelLimitsUnavailableError) as exc:
            raise generation_safety_http_exception(exc) from exc

        initial_title = _date_title()
        try:
            session_id = create_general_chat_session_and_convert_lease(
                db, request_lease_id=lease_id, title=initial_title, first_message=user_message,
            )
        except (RequestLeaseExpiredError, TargetBusyError) as exc:
            raise generation_safety_http_exception(exc) from exc
    except Exception:
        release_lease(db, lease_id)
        raise

    title_task = asyncio.create_task(_generate_title_or_none(db, owner_token, user_message))

    async def event_stream():
        chunks = []
        async for token in llm.generate_from_prepared(prepared):
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

    async def _cleanup():
        # If we got here via the happy path, title_task is already done and this is a no-op.
        # If the stream failed, the client disconnected mid-stream, or the client disconnected
        # before the SSE body was ever iterated at all (see routes/sse.py), the title task may
        # still be running under its OWN request lease -- cancel and await it here so ITS
        # `finally` releases that lease before we release the main conversation lease, never
        # leaving an orphaned title task holding a lease past the end of this request.
        if not title_task.done():
            title_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await title_task
        release_lease(db, lease_id)

    return sse_response(event_stream(), cleanup=_cleanup)


@router.post("/chat/{session_id}/message")
async def send_session_message(request: Request, session_id: int):
    """Acquires the stable per-session thread lease BEFORE reading history or writing any
    message (a concurrent turn on the same session gets a no-write 409), THEN re-checks that the
    session still exists. The lease itself closes the TOCTOU window: delete_chat_session() also
    must acquire this exact same lease before it can delete anything, so once we hold it a
    concurrent delete cannot succeed -- the only way the session can still be "gone" after we
    successfully acquire the lease is if it was already gone before we started, which is a plain
    404, never a mid-flight FK failure. Prepares and preflights the exact reply request BEFORE
    inserting the user message, so an oversized/blocked request never leaves an unanswered user
    turn. The lease is always released once the stream ends, success or failure -- and also if
    the client disconnects before the stream ever begins (see routes/sse.py's sse_response)."""
    db = request.app.state.db
    body = await request.json()
    user_message = body["message"]

    try:
        lease_id = acquire_lease(
            db, conversation_thread_key(session_id), "thread", request.app.state.owner_token
        )
    except (MaintenanceLockedError, TargetBusyError) as exc:
        raise generation_safety_http_exception(exc) from exc

    try:
        if get_chat_session(db, session_id) is None:
            raise HTTPException(status_code=404, detail="session not found")

        preflight_entry_ids = get_ordered_entry_ids(db)
        all_entries = [dict(row) for row in get_entries_in_order(db, preflight_entry_ids)]
        history_rows = db.execute(
            "SELECT role, content FROM chat_message WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()
        history = [{"role": r["role"], "content": r["content"]} for r in history_rows]

        active_prompt = get_active_prompt(db)
        model = active_prompt["model"]
        try:
            prepared = await llm.prepare_general_chat_request(
                all_entries, history, user_message, active_prompt["body_text"], model,
            )
        except (ContextTooLargeError, ModelLimitsUnavailableError) as exc:
            raise generation_safety_http_exception(exc) from exc

        db.execute(
            "INSERT INTO chat_message (thread_kind, session_id, role, content) VALUES ('general', ?, 'user', ?)",
            (session_id, user_message),
        )
    except Exception:
        release_lease(db, lease_id)
        raise

    async def event_stream():
        chunks = []
        async for token in llm.generate_from_prepared(prepared):
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

    return sse_response(event_stream(), cleanup=lambda: release_lease(db, lease_id))
