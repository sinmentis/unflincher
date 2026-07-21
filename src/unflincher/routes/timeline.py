from fastapi import APIRouter, Request

from unflincher.db import (
    get_current_commentary,
    get_entries_with_active_commentary_job,
    get_entry_year_counts,
)
from unflincher.onboarding import IMPORT_DOCS_URL, get_onboarding_state
from unflincher.templates_env import templates
from unflincher.text_metrics import count_writing_units

router = APIRouter()


@router.get("/")
async def timeline(request: Request):
    db = request.app.state.db
    rows = db.execute(
        "SELECT id, title, entry_date, content_text FROM diary_entry ORDER BY entry_date DESC"
    ).fetchall()
    active_job_entry_ids = get_entries_with_active_commentary_job(db)
    entries = []
    for row in rows:
        commentary = get_current_commentary(db, row["id"])
        entries.append({
            "id": row["id"], "title": row["title"], "entry_date": row["entry_date"],
            "year": row["entry_date"][:4], "has_commentary": commentary is not None,
            "is_generating": row["id"] in active_job_entry_ids,
            "word_count": count_writing_units(row["content_text"]),
        })
    total_entries = len(entries)
    years = get_entry_year_counts(db) if total_entries else []
    # Lightweight, data-derived onboarding (see onboarding.py's module docstring and the plan's
    # Lightweight onboarding section) -- no wizard, cookie, or persisted flag; get_onboarding_state
    # is the ONE place that reads existing DB rows and decides what Timeline should show.
    onboarding_state = get_onboarding_state(db)
    context = {
        "entries": entries,
        "years": years,
        "entry_count": total_entries,
        "show_onboarding_panel": onboarding_state.show_start_panel,
        "import_docs_url": IMPORT_DOCS_URL,
    }
    return templates.TemplateResponse(request, "timeline.html", context)
