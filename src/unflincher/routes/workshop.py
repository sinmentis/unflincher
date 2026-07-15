import json
import sqlite3

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from unflincher import llm, regen_enqueue
from unflincher.config import load_settings
from unflincher.context_budget import ContextTooLargeError, ModelLimitsUnavailableError
from unflincher.db import (
    ArchiveChangedError,
    DEFAULT_MODEL,
    ItemJobMismatchError,
    MaintenanceLockedError,
    RequestFormatChangedError,
    StaleOrSupersededRetryError,
    TargetBusyError,
    acquire_lease,
    get_active_prompt,
    get_entries_in_order,
    get_ordered_entry_ids,
    new_request_lease_key,
    release_lease,
    set_active_prompt,
)
from unflincher.i18n import SUPPORTED_LANGUAGE_CODES, t
from unflincher.routes.errors import generation_safety_http_exception
from unflincher.routes.sse import sse_response
from unflincher.sanitize import render_ai_markdown
from unflincher.templates_env import LANG_COOKIE_NAME, get_current_language, templates
from unflincher.worker import BatchWorker

router = APIRouter()


class SetLanguageRequest(BaseModel):
    lang: str


class ApplyAllRequest(BaseModel):
    draft_prompt: str
    model: str


@router.get("/workshop")
async def workshop_page(request: Request):
    db = request.app.state.db
    current_lang = get_current_language(request)
    active_prompt = get_active_prompt(db)
    entries = db.execute("SELECT id, title FROM diary_entry ORDER BY entry_date ASC, id ASC").fetchall()
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
            "supported_languages": [
                (code, t(current_lang, f"language.name.{code}"))
                for code in SUPPORTED_LANGUAGE_CODES
            ],
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

    Acquires a temporary request-scoped lease (see db.new_request_lease_key) BEFORE preparing or
    preflighting anything, so a preview counts as maintenance-aware admitted work the deploy drain
    can observe -- even though it persists nothing. Prepares and preflights the EXACT same
    request llm.build_commentary_envelope/prepare_commentary_request would build for a real
    per-entry trigger, with the exact same full-corpus context (in canonical (entry_date ASC, id
    ASC) order -- see db.get_ordered_entry_ids). Only then does the preview faithfully predict
    the real output AND correctly enforce the same capacity contract before ever opening the SSE
    stream. The single picked entry is the focus, but the model still sees every entry for
    cross-entry pattern matching — passing just the one entry here would make the preview lie
    about what the real generation produces. The lease is released on every path: preflight
    failure, SSE success, SSE failure/disconnect, and even a disconnect that races the SSE
    response before its body ever starts iterating (see routes/sse.py's sse_response).

    An explicit `model` in the body lets the owner trial a model different from the saved active
    one without committing to it (this route still writes nothing); absent that, it falls back to
    the active persona's saved model so the preview matches what a real generation would use.
    """
    db = request.app.state.db
    body = await request.json()

    try:
        lease_id = acquire_lease(db, new_request_lease_key(), "request", request.app.state.owner_token)
    except MaintenanceLockedError as exc:
        raise generation_safety_http_exception(exc) from exc

    try:
        entry = db.execute("SELECT * FROM diary_entry WHERE id = ?", (body["entry_id"],)).fetchone()
        if entry is None:
            raise HTTPException(status_code=404, detail="entry not found")
        preflight_entry_ids = get_ordered_entry_ids(db)
        all_entries = [dict(row) for row in get_entries_in_order(db, preflight_entry_ids)]
        active_prompt = get_active_prompt(db)
        model = body.get("model") or (active_prompt["model"] if active_prompt else DEFAULT_MODEL)

        try:
            prepared = await llm.prepare_commentary_request(
                dict(entry), all_entries, body["draft_prompt"], model,
            )
        except (ContextTooLargeError, ModelLimitsUnavailableError) as exc:
            raise generation_safety_http_exception(exc) from exc
    except Exception:
        release_lease(db, lease_id)
        raise

    async def event_stream():
        chunks = []
        async for token in llm.generate_from_prepared(prepared):
            chunks.append(token)
            yield {"event": "token", "data": token}
        # Preview never persists, but the owner still needs to SEE what a real generation
        # would look like — that means real markdown rendering (bold/paragraphs), not raw
        # tokens frozen in place forever (this route never reloads the page like the
        # persisted commentary/chat/report routes do, so there is no second render pass to
        # fall back on). render_ai_markdown is the exact same sanitizer every persisted
        # surface already uses; sending its output back over the SAME `done` event the client
        # already listens for keeps this a one-mechanism fix rather than a second markdown
        # pipeline.
        full_text = "".join(chunks)
        yield {
            "event": "done",
            "data": json.dumps({"html": render_ai_markdown(full_text)}, ensure_ascii=False),
        }

    return sse_response(event_stream(), cleanup=lambda: release_lease(db, lease_id))


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
async def workshop_apply_all(
    request: Request,
    background_tasks: BackgroundTasks,
    body: ApplyAllRequest | None = None,
):
    """Start one full regeneration job, optionally activating its prompt in the same transaction.

    Prepares and preflights EVERY concrete Entry Reflection request plus the Life Report request
    for the archive as it exists right now (queried fresh, never a hardcoded count), then
    atomically compares that snapshot against the archive at enqueue time, acquires every
    target's exclusive lease, and writes the job — see regen_enqueue.enqueue_apply_all_job. The
    single-flight lock also still lives in the DB: a second job while one is 'running' trips the
    partial unique index -> sqlite3.IntegrityError -> HTTP 409.

    The request body is optional. Legacy/no-body callers regenerate under the already-active prompt
    (unchanged behavior). When the visible workbench posts {draft_prompt, model}, that exact draft
    is activated AND its job is created in one transaction, so a busy 409 saves no prompt. Either
    way, the worker runs against the prompt/model this job actually owns.

    BackgroundTasks (not asyncio.create_task) is deliberate: Starlette runs background tasks to
    completion after the response is sent, and TestClient blocks on them, so the worker's DB writes
    are observable right after the POST returns without real concurrency in tests."""
    db = request.app.state.db
    try:
        if body is None:
            active_prompt = get_active_prompt(db)
            job_id, _ = await regen_enqueue.enqueue_apply_all_job(
                db, persona_text=active_prompt["body_text"], model=active_prompt["model"],
                owner_token=request.app.state.owner_token, activate=False,
                prompt_version_id=active_prompt["id"],
            )
        else:
            job_id, _ = await regen_enqueue.enqueue_apply_all_job(
                db, persona_text=body.draft_prompt, model=body.model,
                owner_token=request.app.state.owner_token, activate=True,
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
    done = sum(1 for item in items if item["status"] == "ok")
    failed_items = [item for item in items if item["status"] == "failed"]
    pending = sum(1 for item in items if item["status"] in ("pending", "running"))
    total = len(items)
    processed = done + len(failed_items)
    progress_bucket = ((processed * 10 + total - 1) // total) if total else 0
    return templates.TemplateResponse(
        request,
        "partials/job_progress.html",
        {
            "job_id": job_id,
            "done": done,
            "failed_count": len(failed_items),
            "failed_items": failed_items,
            "pending": pending,
            "total": total,
            "processed": processed,
            "progress_bucket": progress_bucket,
            "job_status": job["status"] if job else "done",
        },
    )


@router.post("/workshop/jobs/{job_id}/item/{item_id}/retry")
async def retry_job_item(
    request: Request, job_id: int, item_id: int, background_tasks: BackgroundTasks
):
    """Deep retry-admission: regen_enqueue.retry_job_item_with_admission reconstructs the exact
    envelope from the item's immutable prompt + stored snapshot, verifies its assembly
    version/fingerprint still matches what was stored at enqueue time, fetches the model's
    CURRENT published limit and preflights against it, and validates the item actually belongs to
    this URL's job_id -- ALL before ever touching the atomic DB-side retry state. Only once every
    one of those checks succeeds does it call db.retry_failed_job_item (checks maintenance, job
    'done' status, context snapshot, baseline/supersession, no newer same-target work, and
    acquires the target's exclusive lease, all under one BEGIN IMMEDIATE) and return the
    AUTHORITATIVE owning job_id -- this route schedules the worker with THAT id, never blindly
    trusting the URL's job_id. A mismatched job_id/item_id pair is a stable no-write 404; every
    other conflict is a stable no-write 409/413/503. Only after that commit does this route
    schedule the worker; if scheduling or the process itself dies before the worker runs, startup
    recovery (db.recover_or_cancel_running_jobs) owns the already-committed running job and
    lease, exactly like any other recovered job."""
    db = request.app.state.db
    try:
        authoritative_job_id = await regen_enqueue.retry_job_item_with_admission(
            db, item_id=item_id, owner_token=request.app.state.owner_token, expected_job_id=job_id,
        )
    except (
        ContextTooLargeError, ModelLimitsUnavailableError, MaintenanceLockedError,
        TargetBusyError, StaleOrSupersededRetryError, RequestFormatChangedError,
        ItemJobMismatchError,
    ) as exc:
        raise generation_safety_http_exception(exc) from exc
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="a regeneration job is already running")

    settings = load_settings()
    worker = BatchWorker(db, settings.batch_concurrency)
    background_tasks.add_task(worker.run_job, authoritative_job_id)
    # Return the re-rendered progress fragment (not JSON) so htmx's outerHTML swap keeps the panel
    # in place; the job is now 'running', so the fragment re-carries `hx-trigger="every 2s"` and
    # polling resumes automatically.
    return _render_job_progress(request, authoritative_job_id)
