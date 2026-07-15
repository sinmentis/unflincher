from fastapi import APIRouter, Request

from unflincher.db import get_current_commentary, get_entries_with_active_commentary_job
from unflincher.onboarding import IMPORT_DOCS_URL, get_onboarding_state
from unflincher.templates_env import templates

router = APIRouter()


@router.get("/")
async def timeline(request: Request):
    db = request.app.state.db
    rows = db.execute(
        "SELECT id, title, entry_date FROM diary_entry ORDER BY entry_date DESC"
    ).fetchall()
    active_job_entry_ids = get_entries_with_active_commentary_job(db)
    entries = []
    year_counts: dict[str, int] = {}
    for row in rows:
        year = row["entry_date"][:4]
        commentary = get_current_commentary(db, row["id"])
        entries.append({
            "id": row["id"], "title": row["title"], "entry_date": row["entry_date"],
            "year": year, "has_commentary": commentary is not None,
            "is_generating": row["id"] in active_job_entry_ids,
        })
        year_counts[year] = year_counts.get(year, 0) + 1
    # dict preserves insertion order (Python 3.7+); rows are already newest-first, so the first
    # time a year is seen is also its correct sidebar position (newest year first).
    total_entries = len(entries)
    years = [
        {
            "year": year,
            "count": count,
            "density": min(5, max(1, round((count / total_entries) * 5))),
        }
        for year, count in year_counts.items()
    ] if total_entries else []
    # Lightweight, data-derived onboarding (see onboarding.py's module docstring and the plan's
    # Lightweight onboarding section) -- no wizard, cookie, or persisted flag; get_onboarding_state
    # is the ONE place that reads existing DB rows and decides what Timeline should show.
    onboarding_state = get_onboarding_state(db)
    context = {
        "entries": entries,
        "years": years,
        "entry_count": total_entries,
        "date_from": entries[-1]["entry_date"][:10] if entries else None,
        "date_to": entries[0]["entry_date"][:10] if entries else None,
        "show_onboarding_panel": onboarding_state.show_start_panel,
        "import_docs_url": IMPORT_DOCS_URL,
    }
    return templates.TemplateResponse(request, "timeline.html", context)
