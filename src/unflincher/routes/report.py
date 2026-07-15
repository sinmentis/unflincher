from fastapi import APIRouter, HTTPException, Request

from unflincher import llm
from unflincher.context_budget import ContextTooLargeError, ModelLimitsUnavailableError
from unflincher.db import (
    MaintenanceLockedError,
    TargetBusyError,
    acquire_lease,
    get_active_prompt,
    get_current_report,
    get_entries_in_order,
    get_ordered_entry_ids,
    get_report_by_id,
    list_report_versions,
    release_lease,
    report_target_key,
)
from unflincher.i18n import t
from unflincher.perspectives import display_name_key
from unflincher.routes.errors import generation_safety_http_exception
from unflincher.routes.sse import sse_response
from unflincher.sanitize import render_ai_markdown
from unflincher.templates_env import get_current_language, templates

router = APIRouter()


@router.get("/report")
async def report_page(request: Request):
    db = request.app.state.db
    current_lang = get_current_language(request)
    report = get_current_report(db)
    versions = list_report_versions(db)
    context = {
        "report_html": None,
        "report_status": None,
        "report_error": None,
        "report_perspective_name": None,
        "versions": versions,
        "viewing_version_id": None,
    }
    if report:
        context = {
            "report_html": render_ai_markdown(report["body_text"], heading_offset=1),
            "report_status": report["status"],
            "report_error": report["error"],
            "report_perspective_name": t(current_lang, display_name_key(report["prompt_preset_key"])),
            "covered_count": report["covered_entry_count"],
            "covered_from": report["covered_from_date"],
            "covered_to": report["covered_to_date"],
            "versions": versions,
            "viewing_version_id": report["id"],
        }
    return templates.TemplateResponse(request, "report.html", context)


@router.get("/report/{report_id}")
async def view_report_version(request: Request, report_id: int):
    db = request.app.state.db
    report = get_report_by_id(db, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="not found")
    current_lang = get_current_language(request)
    versions = list_report_versions(db)
    return templates.TemplateResponse(
        request,
        "report.html",
        {
            "report_html": (
                render_ai_markdown(report["body_text"], heading_offset=1)
                if report["status"] == "ok"
                else None
            ),
            "report_status": report["status"],
            "report_error": report["error"],
            "report_perspective_name": t(current_lang, display_name_key(report["prompt_preset_key"])),
            "covered_count": report["covered_entry_count"],
            "covered_from": report["covered_from_date"],
            "covered_to": report["covered_to_date"],
            "versions": versions,
            "viewing_version_id": report_id,
        },
    )


@router.post("/report/generate")
async def trigger_report(request: Request):
    """Direct (non-job) Life Report generation. Acquires the report's exclusive lease BEFORE
    preparing the request -- this is what makes a direct report generation and an apply-all job
    mutually exclusive on the SAME report target, not merely "no two direct generations at once".
    Prepares and preflights the exact request BEFORE opening the SSE stream; the lease is always
    released once the stream ends, success or failure."""
    db = request.app.state.db
    try:
        lease_id = acquire_lease(db, report_target_key(), "direct", request.app.state.owner_token)
    except (MaintenanceLockedError, TargetBusyError) as exc:
        raise generation_safety_http_exception(exc) from exc

    try:
        preflight_entry_ids = get_ordered_entry_ids(db)
        all_entries = [dict(row) for row in get_entries_in_order(db, preflight_entry_ids)]
        active_prompt = get_active_prompt(db)
        model = active_prompt["model"]
        try:
            prepared = await llm.prepare_report_request(
                all_entries, active_prompt["body_text"], model
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
        full_text = "".join(chunks)
        dates = [e["entry_date"] for e in all_entries]
        db.execute(
            "INSERT INTO aggregate_report (prompt_version_id, model, body_text, "
            "covered_entry_count, covered_from_date, covered_to_date, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'ok')",
            (
                active_prompt["id"], model, full_text, len(all_entries),
                min(dates) if dates else None, max(dates) if dates else None,
            ),
        )
        yield {"event": "done", "data": "{}"}

    return sse_response(event_stream(), cleanup=lambda: release_lease(db, lease_id))
