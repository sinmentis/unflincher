import json
import sqlite3

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

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
from unflincher.llm import UnsupportedModelError
from unflincher.perspectives import PERSPECTIVE_KEYS, display_name_key, list_presets
from unflincher.routes.errors import generation_safety_http_exception
from unflincher.routes.sse import sse_response
from unflincher.sanitize import render_ai_markdown
from unflincher.templates_env import LANG_COOKIE_NAME, get_current_language, templates
from unflincher.worker import BatchWorker

router = APIRouter()


class SetLanguageRequest(BaseModel):
    lang: str


class _StrictWorkshopRequest(BaseModel):
    """Shared base for the three typed Workshop contracts: rejects ANY field the approved
    contract does not name (Pydantic v2 extra='forbid'), rather than silently ignoring it. This
    is what makes TestRunRequest's "never accepts a preset_key" guarantee an enforced 422, not
    merely an unused field a client could still send without complaint."""

    model_config = ConfigDict(extra="forbid")


class TestRunRequest(_StrictWorkshopRequest):
    """Preview contract: ONLY entry_id, draft_prompt, and model -- no preset_key field exists at
    all, so sending one is a stable 422 (see _StrictWorkshopRequest). model is optional: omitting
    it falls back to the active persona's saved model, matching a real trigger."""

    entry_id: int
    draft_prompt: str
    model: str | None = None


class ApplyRequest(_StrictWorkshopRequest):
    """Apply is save-only. preset_key is an optional caller-claimed INTENT hint only -- the
    persisted value always comes from perspectives.classify_prompt(draft_prompt) via
    db.set_active_prompt, so a stale/forged/edited hint can never misclassify the stored text."""

    draft_prompt: str
    model: str
    preset_key: str | None = None


class ApplyAllRequest(_StrictWorkshopRequest):
    """Same optional preset_key contract as ApplyRequest (see its docstring)."""

    draft_prompt: str
    model: str
    preset_key: str | None = None


def _reject_if_empty_instructions(draft_prompt: str) -> None:
    """Stable 400 for empty/whitespace-only instructions. Only INSPECTS the text -- the value
    that is actually stored elsewhere is never stripped or normalized."""
    if not draft_prompt.strip():
        raise HTTPException(status_code=400, detail={"reason": "empty_instructions"})


def _reject_if_unknown_preset_key(preset_key: str | None) -> None:
    """A claimed preset key that isn't even one of the shipped keys can never be legitimate
    intent (see perspectives.classify_prompt) -- reject it outright with a stable 400 rather
    than silently ignoring it."""
    if preset_key is not None and preset_key not in PERSPECTIVE_KEYS:
        raise HTTPException(
            status_code=400, detail={"reason": "unknown_preset_key", "preset_key": preset_key},
        )


async def _reject_if_unsupported_model(model: str, active_model: str) -> None:
    """See llm.validate_selected_model: accepts the currently active model even during a
    catalog outage, otherwise requires a changed model to exist in the latest catalog."""
    try:
        await llm.validate_selected_model(model, active_model)
    except (UnsupportedModelError, ModelLimitsUnavailableError) as exc:
        raise generation_safety_http_exception(exc) from exc


def _resolve_active_model(db) -> str:
    active_prompt = get_active_prompt(db)
    return active_prompt["model"] if active_prompt else DEFAULT_MODEL


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

    active_model = active_prompt["model"] if active_prompt else DEFAULT_MODEL
    # The active model must always be selectable, even when a (possibly stale) catalog fetch
    # doesn't include it -- otherwise the <select> silently defaults to whatever the browser
    # picks first, and a save without changing the model would still change it. Falls back to
    # the raw model ID as its own display text since a discovery outage means no display name is
    # available for it either.
    if active_model not in {model_id for model_id, _ in models}:
        models = [(active_model, active_model), *models]

    # Perspective radio-card data (Task: Workshop). Presets are rendered from perspectives.py's
    # exact shipped text -- the browser never reconstructs prompt text itself (see workshop.js).
    # Names/descriptions reuse the perspective.<key>.name/.description i18n keys already shipped
    # for the later per-entry/report display, rather than duplicating terminology.
    presets = [
        {
            "key": preset.key,
            "name": t(current_lang, preset.name_key),
            "description": t(current_lang, preset.description_key),
            "prompt": preset.prompt,
        }
        for preset in list_presets()
    ]
    # NULL, any legacy row, and any UNKNOWN stored key (e.g. a since-removed historical preset)
    # are all Custom for rendering purposes -- never retrospectively classified by body text
    # here, and never left as a value with no matching radio (which would leave every radio
    # unchecked).
    raw_preset_key = active_prompt["preset_key"] if active_prompt else None
    active_preset_key = raw_preset_key if raw_preset_key in PERSPECTIVE_KEYS else None
    custom_name = t(current_lang, "perspective.custom.name")
    # display_name_key folds the same NULL/unknown-is-Custom rule used by the entry/report/chat
    # Perspective indicators (see perspectives.display_name_key) -- one shared resolution instead
    # of two separate "is this key Custom" implementations.
    active_perspective_name = t(current_lang, display_name_key(raw_preset_key))
    # Client-side data blob (Task: Workshop) -- workshop.js reads exact preset text/names from
    # here so it never reconstructs prompt text itself (see workshop.js's preset-fill handler).
    # "custom" has no prompt text: selecting it never overwrites the textarea.
    perspective_data = {
        preset["key"]: {"prompt": preset["prompt"], "name": preset["name"]} for preset in presets
    }
    perspective_data["custom"] = {"prompt": None, "name": custom_name}

    return templates.TemplateResponse(
        request,
        "workshop.html",
        {
            "active_prompt": active_prompt["body_text"] if active_prompt else "",
            "active_model": active_model,
            "models": models,
            "models_error": models_error,
            "entries": entries,
            "presets": presets,
            "active_preset_key": active_preset_key,
            "custom_name": custom_name,
            "custom_description": t(current_lang, "perspective.custom.description"),
            "active_perspective_name": active_perspective_name,
            "perspective_data": perspective_data,
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
async def workshop_test_run(request: Request, body: TestRunRequest):
    """Preview only — NEVER writes to the database, not even a log line with the draft text, and
    NEVER accepts/persists a preset_key (see TestRunRequest).

    Entry existence, instructions, and the selected model are all validated BEFORE opening the
    SSE response or acquiring the request lease -- a missing entry, empty instructions, or an
    unsupported/unvalidatable model must never touch generation-safety admission at all.

    Only once those pass does this acquire a temporary request-scoped lease (see
    db.new_request_lease_key), so a preview counts as maintenance-aware admitted work the deploy
    drain can observe -- even though it persists nothing. It then prepares and preflights the
    EXACT same request llm.build_commentary_envelope/prepare_commentary_request would build for a
    real per-entry trigger, with the same target-bounded context derived from canonical
    (entry_date ASC, id ASC) order (see db.get_ordered_entry_ids). The selected entry sees itself
    and earlier entries, never later writing. Only then does the preview faithfully predict the
    real output AND correctly enforce the same capacity contract before ever opening the SSE
    stream. The lease is released on every path: preflight failure, SSE success, SSE
    failure/disconnect, and even a disconnect that races the SSE response before its body ever
    starts iterating (see routes/sse.py's sse_response).

    An explicit `model` in the body lets the owner trial a model different from the saved active
    one without committing to it (this route still writes nothing); absent that, it falls back to
    the active persona's saved model so the preview matches what a real generation would use.
    """
    db = request.app.state.db

    _reject_if_empty_instructions(body.draft_prompt)
    entry = db.execute("SELECT * FROM diary_entry WHERE id = ?", (body.entry_id,)).fetchone()
    if entry is None:
        raise HTTPException(status_code=404, detail={"reason": "entry_not_found"})
    active_model = _resolve_active_model(db)
    model = body.model or active_model
    await _reject_if_unsupported_model(model, active_model)

    try:
        lease_id = acquire_lease(db, new_request_lease_key(), "request", request.app.state.owner_token)
    except MaintenanceLockedError as exc:
        raise generation_safety_http_exception(exc) from exc

    try:
        preflight_entry_ids = get_ordered_entry_ids(db)
        all_entries = [dict(row) for row in get_entries_in_order(db, preflight_entry_ids)]

        try:
            prepared = await llm.prepare_commentary_request(
                dict(entry), all_entries, body.draft_prompt, model,
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
async def workshop_apply(request: Request, body: ApplyRequest):
    """Commit the draft as the new active persona version. No generation happens here — that is
    the apply-to-all path below. This is purely a version swap, save-only. The chosen model is
    persisted as part of the new version, so every later generation under it uses that model.

    Validates instructions/preset key/model BEFORE writing anything (see the module-level
    helpers). body.preset_key is only an intent hint -- the persisted value always comes from
    perspectives.classify_prompt(body.draft_prompt) via set_active_prompt, never the claim (see
    ApplyRequest's docstring). The response's own preset_key is read back from the ACTUAL
    inserted row (not re-derived by calling classify_prompt again here) so the persistence module
    stays the one authoritative place classification happens -- a future change there can never
    silently drift from what this route reports."""
    db = request.app.state.db
    _reject_if_empty_instructions(body.draft_prompt)
    _reject_if_unknown_preset_key(body.preset_key)
    active_model = _resolve_active_model(db)
    await _reject_if_unsupported_model(body.model, active_model)
    new_id = set_active_prompt(db, body.draft_prompt, body.model, preset_key=body.preset_key)
    stored_preset_key = db.execute(
        "SELECT preset_key FROM persona_prompt WHERE id = ?", (new_id,)
    ).fetchone()["preset_key"]
    return {"persona_prompt_id": new_id, "preset_key": stored_preset_key}


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
    are observable right after the POST returns without real concurrency in tests.

    When the visible workbench posts an explicit body, instructions/preset key/model are all
    validated (see the module-level helpers) BEFORE any preflight or write -- a busy/capacity
    rollback then saves neither the prompt nor its preset key. The response's own preset_key is
    read back from the ACTUAL persisted row (the activated row for an explicit body, or the
    already-active row for the legacy no-body path) rather than re-derived by calling
    classify_prompt again here, so the persistence module stays the one authoritative place
    classification happens. A compatible upgraded database may have zero persona_prompt rows; the
    legacy no-body path then has nothing to regenerate under and returns a stable 409 telling the
    caller to Apply a prompt first, rather than crashing on a null active prompt."""
    db = request.app.state.db
    try:
        if body is None:
            active_prompt = get_active_prompt(db)
            if active_prompt is None:
                raise HTTPException(status_code=409, detail={"reason": "no_active_prompt"})
            resolved_preset_key = active_prompt["preset_key"]
            job_id, _ = await regen_enqueue.enqueue_apply_all_job(
                db, persona_text=active_prompt["body_text"], model=active_prompt["model"],
                owner_token=request.app.state.owner_token, activate=False,
                prompt_version_id=active_prompt["id"],
            )
        else:
            _reject_if_empty_instructions(body.draft_prompt)
            _reject_if_unknown_preset_key(body.preset_key)
            active_model = _resolve_active_model(db)
            await _reject_if_unsupported_model(body.model, active_model)
            job_id, activated_prompt_id = await regen_enqueue.enqueue_apply_all_job(
                db, persona_text=body.draft_prompt, model=body.model,
                owner_token=request.app.state.owner_token, activate=True,
                activate_preset_key=body.preset_key,
            )
            resolved_preset_key = db.execute(
                "SELECT preset_key FROM persona_prompt WHERE id = ?", (activated_prompt_id,)
            ).fetchone()["preset_key"]
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
    return JSONResponse({"job_id": job_id, "preset_key": resolved_preset_key})


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
