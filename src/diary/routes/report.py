from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from diary import llm
from diary.config import load_settings
from diary.db import get_active_prompt, get_current_report, get_report_by_id, list_report_versions
from diary.sanitize import render_ai_markdown

router = APIRouter()
templates = Jinja2Templates(directory="src/diary/templates")


@router.get("/report")
async def report_page(request: Request):
    db = request.app.state.db
    report = get_current_report(db)
    versions = list_report_versions(db)
    context = {"report_html": None, "versions": versions, "viewing_version_id": None}
    if report:
        context = {
            "report_html": render_ai_markdown(report["body_text"]),
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
    versions = list_report_versions(db)
    return templates.TemplateResponse(
        request,
        "report.html",
        {
            "report_html": render_ai_markdown(report["body_text"]) if report["status"] == "ok" else None,
            "covered_count": report["covered_entry_count"],
            "covered_from": report["covered_from_date"],
            "covered_to": report["covered_to_date"],
            "versions": versions,
            "viewing_version_id": report_id,
        },
    )


@router.post("/report/generate")
async def trigger_report(request: Request):
    db = request.app.state.db
    all_entries = db.execute("SELECT * FROM diary_entry ORDER BY entry_date").fetchall()
    active_prompt = get_active_prompt(db)
    settings = load_settings()

    async def event_stream():
        chunks = []
        async for token in llm.generate_report(
            [dict(e) for e in all_entries], active_prompt["body_text"], settings.llm_model
        ):
            chunks.append(token)
            yield {"event": "token", "data": token}
        full_text = "".join(chunks)
        dates = [e["entry_date"] for e in all_entries]
        db.execute(
            "INSERT INTO aggregate_report (prompt_version_id, model, body_text, "
            "covered_entry_count, covered_from_date, covered_to_date, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'ok')",
            (
                active_prompt["id"], settings.llm_model, full_text, len(all_entries),
                min(dates) if dates else None, max(dates) if dates else None,
            ),
        )
        yield {"event": "done", "data": "{}"}

    return EventSourceResponse(event_stream(), sep="\n")
