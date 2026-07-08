from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from diary import llm
from diary.config import load_settings
from diary.db import get_active_prompt, get_current_report
from diary.sanitize import render_ai_markdown

router = APIRouter()
templates = Jinja2Templates(directory="src/diary/templates")


@router.get("/report")
async def report_page(request: Request):
    db = request.app.state.db
    report = get_current_report(db)
    context = {"report_html": None}
    if report:
        context = {
            "report_html": render_ai_markdown(report["body_text"]),
            "covered_count": report["covered_entry_count"],
            "covered_from": report["covered_from_date"],
            "covered_to": report["covered_to_date"],
        }
    return templates.TemplateResponse(request, "report.html", context)


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
