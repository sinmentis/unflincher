import json
import sqlite3

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from diary import llm
from diary.config import load_settings
from diary.db import DEFAULT_MODEL, get_active_prompt, set_active_prompt, start_regen_job
from diary.i18n import SUPPORTED_LANGUAGE_CODES
from diary.sanitize import render_ai_markdown
from diary.templates_env import LANG_COOKIE_NAME, templates
from diary.worker import BatchWorker

router = APIRouter()


class SetLanguageRequest(BaseModel):
    lang: str


@router.get("/workshop")
async def workshop_page(request: Request):
    db = request.app.state.db
    active_prompt = get_active_prompt(db)
    entries = db.execute("SELECT id, title FROM diary_entry ORDER BY entry_date").fetchall()
    models: list[tuple[str, str]] = []
    models_error: str | None = None
    try:
        models = await llm.list_available_models()
    except Exception as exc:
        models_error = str(exc)
    return templates.TemplateResponse(
        request,
        "workshop.html",
        {
            "active_prompt": active_prompt["body_text"] if active_prompt else "",
            "active_model": active_prompt["model"] if active_prompt else DEFAULT_MODEL,
            "models": models,
            "models_error": models_error,
            "entries": entries,
        },
    )


@router.post("/workshop/refresh-models")
async def refresh_models(request: Request):
    try:
        await llm.refresh_available_models()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


@router.post("/workshop/language")
async def set_language(request: Request, body: SetLanguageRequest):
    if body.lang not in SUPPORTED_LANGUAGE_CODES:
        raise HTTPException(status_code=400, detail=f"unsupported language: {body.lang}")
    response = JSONResponse({"ok": True})
    response.set_cookie(LANG_COOKIE_NAME, body.lang, httponly=False, samesite="strict")
    return response


@router.post("/workshop/test-run")
async def workshop_test_run(request: Request):
    """Preview only — NEVER writes to the database, not even a log line with the draft text.

    Runs the exact same llm.generate_commentary function, with the exact same full-corpus
    context, as the real per-entry trigger (Task 9). Only then does the preview faithfully
    predict the real output. The single picked entry is the focus, but the model still sees
    every entry for cross-entry pattern matching — passing just the one entry here would make
    the preview lie about what the real generation produces.

    An explicit `model` in the body lets the owner trial a model different from the saved active
    one without committing to it (this route still writes nothing); absent that, it falls back to
    the active persona's saved model so the preview matches what a real generation would use.
    """
    db = request.app.state.db
    body = await request.json()
    entry = db.execute("SELECT * FROM diary_entry WHERE id = ?", (body["entry_id"],)).fetchone()
    all_entries = db.execute("SELECT * FROM diary_entry ORDER BY entry_date").fetchall()
    active_prompt = get_active_prompt(db)
    model = body.get("model") or (active_prompt["model"] if active_prompt else DEFAULT_MODEL)

    async def event_stream():
        chunks = []
        async for token in llm.generate_commentary(
            dict(entry),
            [dict(e) for e in all_entries],
            body["draft_prompt"],
            model,
        ):
            chunks.append(token)
            yield {"event": "token", "data": token}
        # Preview never persists, but the owner still needs to SEE what a real generation would
        # look like — that means real markdown rendering (bold/paragraphs), not raw tokens frozen
        # in place forever (this route never reloads the page like the persisted commentary/chat/
        # report routes do, so there is no second render pass to fall back on). render_ai_markdown
        # is the exact same sanitizer every persisted surface already uses; sending its output back
        # over the SAME `done` event the client already listens for keeps this a one-mechanism fix
        # rather than a second markdown pipeline.
        full_text = "".join(chunks)
        yield {
            "event": "done",
            "data": json.dumps({"html": render_ai_markdown(full_text)}, ensure_ascii=False),
        }

    return EventSourceResponse(event_stream(), sep="\n")


@router.post("/workshop/apply")
async def workshop_apply(request: Request):
    """Commit the draft as the new active persona version. No generation happens here — that is
    Task 16's apply-to-all path. This is purely a version swap. The chosen model is persisted as
    part of the new version, so every later generation under it uses that model."""
    db = request.app.state.db
    body = await request.json()
    new_id = set_active_prompt(db, body["draft_prompt"], body["model"])
    return {"persona_prompt_id": new_id}


@router.post("/workshop/apply-all")
async def workshop_apply_all(request: Request, background_tasks: BackgroundTasks):
    """Regenerate commentary for EVERY diary entry that exists right now, plus the aggregate
    report, under the active persona. Scope is queried fresh (never a hardcoded count) so a new
    entry added a second ago is included. The single-flight lock lives in the DB: a second job
    while one is 'running' trips the partial unique index → sqlite3.IntegrityError → HTTP 409.

    BackgroundTasks (not asyncio.create_task) is deliberate: Starlette runs background tasks to
    completion after the response is sent, and TestClient blocks on them, so the worker's DB
    writes are observable right after the POST returns without real concurrency in tests."""
    db = request.app.state.db
    active_prompt = get_active_prompt(db)
    entry_ids = [r["id"] for r in db.execute("SELECT id FROM diary_entry").fetchall()]
    try:
        job_id = start_regen_job(db, active_prompt["id"], entry_ids)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="a regeneration job is already running")

    settings = load_settings()
    worker = BatchWorker(db, settings.batch_concurrency)
    background_tasks.add_task(
        worker.run_job, job_id, active_prompt["body_text"], active_prompt["model"]
    )
    return JSONResponse({"job_id": job_id})


@router.get("/workshop/jobs/{job_id}/progress")
async def job_progress(request: Request, job_id: int):
    """htmx-polled fragment: per-item counts plus any failed items (each with a retry button)."""
    return _render_job_progress(request, job_id)


def _render_job_progress(request: Request, job_id: int):
    """Render the per-job progress fragment from the current item/job state. Shared by the polled
    GET route and the retry POST so both return the same HTML (with the `every 2s` polling
    attribute whenever the job is 'running'), never divergent bodies."""
    db = request.app.state.db
    items = db.execute(
        "SELECT * FROM regen_job_item WHERE job_id = ?", (job_id,)
    ).fetchall()
    job = db.execute("SELECT status FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    done = sum(1 for i in items if i["status"] == "ok")
    failed_items = [i for i in items if i["status"] == "failed"]
    pending = sum(1 for i in items if i["status"] in ("pending", "running"))
    return templates.TemplateResponse(
        request,
        "partials/job_progress.html",
        {
            "job_id": job_id,
            "done": done,
            "failed_count": len(failed_items),
            "failed_items": failed_items,
            "pending": pending,
            "total": len(items),
            "job_status": job["status"] if job else "done",
        },
    )


@router.post("/workshop/jobs/{job_id}/item/{item_id}/retry")
async def retry_job_item(
    request: Request, job_id: int, item_id: int, background_tasks: BackgroundTasks
):
    """Re-queue one failed item and reopen the (already 'done') job so the worker drives it
    again. Same BackgroundTasks discipline as apply-all."""
    db = request.app.state.db
    db.execute(
        "UPDATE regen_job_item SET status = 'pending', updated_at = datetime('now') "
        "WHERE id = ? AND job_id = ? AND status = 'failed'",
        (item_id, job_id),
    )
    db.execute(
        "UPDATE regen_job SET status = 'running', finished_at = NULL "
        "WHERE id = ? AND status = 'done'",
        (job_id,),
    )
    job = db.execute("SELECT * FROM regen_job WHERE id = ?", (job_id,)).fetchone()
    prompt = db.execute(
        "SELECT body_text, model FROM persona_prompt WHERE id = ?", (job["prompt_version_id"],)
    ).fetchone()
    settings = load_settings()
    worker = BatchWorker(db, settings.batch_concurrency)
    # Retry with the model this job's own persona version carried, NOT the currently-active
    # persona's model (which may have changed since the job started). A retried item must stay
    # consistent with the rest of its job.
    background_tasks.add_task(
        worker.run_job, job_id, prompt["body_text"], prompt["model"]
    )
    # Return the re-rendered progress fragment (not JSON) so htmx's outerHTML swap keeps the panel
    # in place; the job is now 'running', so the fragment re-carries `hx-trigger="every 2s"` and
    # polling resumes automatically.
    return _render_job_progress(request, job_id)
