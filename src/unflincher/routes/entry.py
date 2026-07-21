import sqlite3

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import Response

from unflincher import llm, regen_enqueue
from unflincher.config import load_settings
from unflincher.context_budget import ContextTooLargeError, ModelLimitsUnavailableError
from unflincher.db import (
    ArchiveChangedError,
    MaintenanceLockedError,
    TargetBusyError,
    acquire_lease,
    entry_thread_key,
    get_active_prompt,
    get_adjacent_entries,
    get_current_commentary,
    get_latest_commentary_job_item,
    release_lease,
)
from unflincher.i18n import t
from unflincher.perspectives import display_name_key
from unflincher.routes.errors import generation_safety_http_exception
from unflincher.routes.sse import sse_response
from unflincher.sanitize import render_ai_markdown
from unflincher.templates_env import get_current_language, templates
from unflincher.worker import BatchWorker

router = APIRouter()


def _perspective_name(lang: str, preset_key: str | None) -> str:
    """Resolve the localized Perspective display name for a joined `preset_key` (NULL or an
    unrecognized/historical key both render as Custom -- see perspectives.display_name_key)."""
    return t(lang, display_name_key(preset_key))


@router.get("/entry/{entry_id}")
async def entry_detail(request: Request, entry_id: int):
    db = request.app.state.db
    entry = db.execute("SELECT * FROM diary_entry WHERE id = ?", (entry_id,)).fetchone()
    if entry is None:
        raise HTTPException(status_code=404, detail="entry not found")

    current_lang = get_current_language(request)
    commentary = get_current_commentary(db, entry_id)
    commentary_html = render_ai_markdown(commentary["body_text"]) if commentary else None
    commentary_perspective_name = (
        _perspective_name(current_lang, commentary["prompt_preset_key"]) if commentary else None
    )
    active_prompt = get_active_prompt(db)
    next_response_perspective_name = _perspective_name(
        current_lang, active_prompt["preset_key"] if active_prompt else None
    )
    chat_history = db.execute(
        "SELECT role, content, created_at FROM chat_message "
        "WHERE thread_kind='entry' AND entry_id=? ORDER BY id",
        (entry_id,),
    ).fetchall()
    # AI replies are markdown (the model writes **bold**/paragraphs); user turns are plain typed
    # text and must stay through Jinja2's default auto-escaping, not the markdown pipeline.
    chat_history = [
        {
            "role": m["role"],
            "content": m["content"],
            "content_html": render_ai_markdown(m["content"]) if m["role"] == "assistant" else None,
            "created_at": m["created_at"],
        }
        for m in chat_history
    ]

    latest_item = get_latest_commentary_job_item(db, entry_id)
    commentary_job_status = latest_item["status"] if latest_item else None
    commentary_job_error = (
        latest_item["error"] if latest_item and latest_item["status"] == "failed" else None
    )
    prev_entry, next_entry = get_adjacent_entries(db, entry_id, entry["entry_date"])
    word_count = len(entry["content_text"].split())

    return templates.TemplateResponse(
        request,
        "entry_detail.html",
        {
            "entry": entry,
            "word_count": word_count,
            "prev_entry": prev_entry,
            "next_entry": next_entry,
            "commentary_html": commentary_html,
            "commentary_perspective_name": commentary_perspective_name,
            "next_response_perspective_name": next_response_perspective_name,
            "chat_history": chat_history,
            "commentary_job_status": commentary_job_status,
            "commentary_job_error": commentary_job_error,
        },
    )


@router.post("/entry/{entry_id}/commentary")
async def trigger_entry_commentary(request: Request, entry_id: int, background_tasks: BackgroundTasks):
    """Fire-and-forget: prepares and preflights the exact Entry Reflection request this entry
    would generate (full-archive context, exactly like a real generation), then atomically
    compares the archive snapshot, acquires this entry's exclusive lease, and writes a
    single-item snapshot-backed job -- see regen_enqueue.enqueue_single_entry_job. Returns
    immediately so the caller (the browser) can navigate away without losing the result."""
    db = request.app.state.db
    entry = db.execute("SELECT * FROM diary_entry WHERE id = ?", (entry_id,)).fetchone()
    if entry is None:
        raise HTTPException(status_code=404, detail="entry not found")

    active_prompt = get_active_prompt(db)
    try:
        job_id = await regen_enqueue.enqueue_single_entry_job(
            db, entry_id=entry_id, prompt_version_id=active_prompt["id"],
            persona_text=active_prompt["body_text"], model=active_prompt["model"],
            owner_token=request.app.state.owner_token,
        )
    except (
        ContextTooLargeError, ModelLimitsUnavailableError, MaintenanceLockedError,
        TargetBusyError, ArchiveChangedError,
    ) as exc:
        raise generation_safety_http_exception(exc) from exc
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="a regeneration job is already running")

    settings = load_settings()
    worker = BatchWorker(db, settings.batch_concurrency)
    background_tasks.add_task(worker.run_job, job_id)
    return {"job_id": job_id}


@router.get("/entry/{entry_id}/commentary-status")
async def entry_commentary_status(request: Request, entry_id: int):
    """Polled by the entry page's busy-state widget every few seconds. While a job item is
    pending/running it re-renders the "still generating" fragment (whose hx-trigger keeps the
    poll alive). Once the job is no longer busy it returns 204 with `HX-Refresh: true`, which
    tells htmx to reload the whole page so the freshly server-rendered commentary shows -- see
    partials/commentary_status.html."""
    db = request.app.state.db
    item = get_latest_commentary_job_item(db, entry_id)
    busy = item is not None and item["status"] in ("pending", "running")
    if not busy:
        return Response(status_code=204, headers={"HX-Refresh": "true"})
    return templates.TemplateResponse(
        request, "partials/commentary_status.html", {"entry_id": entry_id, "busy": True}
    )


@router.post("/entry/{entry_id}/chat")
async def entry_chat(request: Request, entry_id: int):
    """Acquires the stable per-entry thread lease BEFORE reading history or writing any message
    (a concurrent turn on the same entry thread gets a no-write 409 -- see
    db.entry_thread_key/db.acquire_lease), then prepares and preflights the exact reply request
    BEFORE inserting the user message, so an oversized/blocked request never leaves an
    unanswered user turn in the thread. The lease is always released once the stream ends,
    success or failure -- and also if the client disconnects before the stream ever begins (see
    routes/sse.py's sse_response, which covers that race the generator's own cleanup cannot)."""
    db = request.app.state.db
    entry = db.execute("SELECT * FROM diary_entry WHERE id = ?", (entry_id,)).fetchone()
    if entry is None:
        raise HTTPException(status_code=404, detail="entry not found")
    body = await request.json()
    user_message = body["message"]

    try:
        lease_id = acquire_lease(
            db, entry_thread_key(entry_id), "thread", request.app.state.owner_token
        )
    except (MaintenanceLockedError, TargetBusyError) as exc:
        raise generation_safety_http_exception(exc) from exc

    try:
        # The thread is keyed by entry_id alone, so history spans every past exchange regardless
        # of which entry_commentary version was current at the time. Read it before the new user
        # turn so the model sees prior turns, not the message it is about to answer.
        history_rows = db.execute(
            "SELECT role, content FROM chat_message WHERE thread_kind='entry' AND entry_id=? ORDER BY id",
            (entry_id,),
        ).fetchall()
        history = [{"role": r["role"], "content": r["content"]} for r in history_rows]

        # Always ground the reply in the LATEST status='ok' commentary, never a specific/viewed
        # version — get_current_commentary already returns the newest ok row.
        commentary = get_current_commentary(db, entry_id)
        commentary_text = commentary["body_text"] if commentary else None
        active_prompt = get_active_prompt(db)
        model = active_prompt["model"]

        try:
            prepared = await llm.prepare_entry_chat_request(
                dict(entry), commentary_text, history, user_message, active_prompt["body_text"], model,
            )
        except (ContextTooLargeError, ModelLimitsUnavailableError) as exc:
            raise generation_safety_http_exception(exc) from exc

        # Durable write happens only AFTER preflight succeeds.
        db.execute(
            "INSERT INTO chat_message (thread_kind, entry_id, role, content) VALUES ('entry', ?, 'user', ?)",
            (entry_id, user_message),
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
            "INSERT INTO chat_message (thread_kind, entry_id, role, content, model) "
            "VALUES ('entry', ?, 'assistant', ?, ?)",
            (entry_id, full_text, model),
        )
        yield {"event": "done", "data": "{}"}

    return sse_response(event_stream(), cleanup=lambda: release_lease(db, lease_id))
