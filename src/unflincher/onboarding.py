"""Derives lightweight onboarding guidance entirely from existing database state (see the plan's
Lightweight onboarding section). There is no wizard, tutorial table, cookie, localStorage key, or
persisted "seen onboarding" flag anywhere in this app -- every render asks this ONE small interface
what to show, instead of Timeline/Life Report templates and routes each scattering their own
COUNT/EXISTS queries and re-deriving the same three states independently.

Stage encodes exactly three data-derived states:
- NO_ENTRIES: the Journal Archive is empty.
- READY_TO_REFLECT: entries exist, but no Entry Reflection or Life Report has ever completed
  successfully. A row with status='failed' does NOT count -- this mirrors db.py's own
  "current" == latest status='ok' row rule (see get_current_commentary/get_current_report), so
  a failed-only generation never looks like completed onboarding.
- ACTIVE: at least one successful (status='ok') Entry Reflection or Life Report exists anywhere
  in the archive. The onboarding panel is removed and Timeline behaves normally.

Onboarding never blocks browsing, writing, Prompt Workshop, or generation -- this module only
answers "what should the panel say", it never gates a route or action.
"""
import sqlite3
from dataclasses import dataclass

NO_ENTRIES = "no_entries"
READY_TO_REFLECT = "ready_to_reflect"
ACTIVE = "active"

# The one place the private app links out to the documented, CLI-only Douban Excel import flow
# (see docs/import.md). Import stays CLI-only this cycle (Locked decision 8) -- this is a plain
# external link, never a browser upload form or a claim of generic Excel/Day One/Notion/Google
# Docs support.
IMPORT_DOCS_URL = "https://github.com/sinmentis/unflincher/blob/main/docs/import.md"


@dataclass(frozen=True)
class OnboardingState:
    stage: str
    entry_count: int

    @property
    def show_start_panel(self) -> bool:
        """True only for READY_TO_REFLECT -- the compact three-step panel Timeline shows once
        entries exist but nothing has successfully generated yet. NO_ENTRIES gets its own empty
        state (see timeline.html); ACTIVE shows the normal Timeline with no panel at all."""
        return self.stage == READY_TO_REFLECT


def get_onboarding_state(conn: sqlite3.Connection) -> OnboardingState:
    entry_count = conn.execute("SELECT COUNT(*) AS n FROM diary_entry").fetchone()["n"]
    if entry_count == 0:
        return OnboardingState(stage=NO_ENTRIES, entry_count=0)

    has_ok_commentary = conn.execute(
        "SELECT 1 FROM entry_commentary WHERE status = 'ok' LIMIT 1"
    ).fetchone() is not None
    has_ok_report = conn.execute(
        "SELECT 1 FROM aggregate_report WHERE status = 'ok' LIMIT 1"
    ).fetchone() is not None

    if has_ok_commentary or has_ok_report:
        return OnboardingState(stage=ACTIVE, entry_count=entry_count)

    return OnboardingState(stage=READY_TO_REFLECT, entry_count=entry_count)
