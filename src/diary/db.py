"""SQLite (WAL) schema and the core query helpers every route/worker task builds on.

Design invariants enforced here (see plan Global Constraints):
- Only one persona_prompt row may have is_active=1 at a time (partial unique index).
- Only one regen_job may be status='running' at a time (partial unique index).
- "Current" commentary/report = latest row WHERE status='ok' (never plain latest-by-date).
- complete_job_item() writes the result row and flips the job item to 'ok' in ONE transaction.
"""
import sqlite3
from datetime import datetime, timezone

# Fallback model for a brand-new install and for rows that predate the persona_prompt.model
# column (see migrate_persona_prompt_model). Kept in sync with config.py's DIARY_LLM_MODEL
# default so upgrading an existing deployment leaves generation behaviour unchanged.
DEFAULT_MODEL = "claude-sonnet-4.6"

SCHEMA = """
CREATE TABLE IF NOT EXISTS diary_entry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content_html_raw TEXT NOT NULL,
    content_html TEXT NOT NULL,
    content_text TEXT NOT NULL,
    entry_date TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('import', 'manual')),
    douban_url TEXT,
    source_modified_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS persona_prompt (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_no INTEGER NOT NULL,
    body_text TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_persona_prompt_one_active
    ON persona_prompt (is_active) WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS entry_commentary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL REFERENCES diary_entry(id),
    prompt_version_id INTEGER NOT NULL REFERENCES persona_prompt(id),
    model TEXT NOT NULL,
    body_text TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_entry_commentary_entry_id ON entry_commentary (entry_id, created_at DESC);

CREATE TABLE IF NOT EXISTS aggregate_report (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_version_id INTEGER NOT NULL REFERENCES persona_prompt(id),
    model TEXT NOT NULL,
    body_text TEXT NOT NULL,
    covered_entry_count INTEGER NOT NULL,
    covered_from_date TEXT,
    covered_to_date TEXT,
    status TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_kind TEXT NOT NULL CHECK (thread_kind IN ('entry', 'general')),
    entry_id INTEGER REFERENCES diary_entry(id),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    model TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_chat_message_thread ON chat_message (thread_kind, entry_id, created_at);

CREATE TABLE IF NOT EXISTS chat_session (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS regen_job (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_version_id INTEGER NOT NULL REFERENCES persona_prompt(id),
    status TEXT NOT NULL CHECK (status IN ('running', 'done', 'cancelled')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_regen_job_one_running
    ON regen_job (status) WHERE status = 'running';

CREATE TABLE IF NOT EXISTS regen_job_item (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES regen_job(id),
    target_type TEXT NOT NULL CHECK (target_type IN ('entry_commentary', 'aggregate_report')),
    entry_id INTEGER REFERENCES diary_entry(id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'ok', 'failed')) DEFAULT 'pending',
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    result_id INTEGER,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_regen_job_item_job_id ON regen_job_item (job_id, status);
"""


def get_connection(db_path: str) -> sqlite3.Connection:
    # check_same_thread=False: the app uses one connection stored on app.state, shared across
    # async route handlers and the background worker task, all of which run on the single
    # uvicorn --workers 1 event-loop thread (see Global Constraints) — never a real multi-thread
    # writer scenario, this just avoids sqlite3's default same-thread assertion tripping in tests.
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def migrate_persona_prompt_model(conn: sqlite3.Connection) -> None:
    """Add persona_prompt.model to a database created before the column existed.

    This is the app's first real schema migration. init_schema() only runs CREATE TABLE IF NOT
    EXISTS, which is a no-op against the already-deployed production table, so the new column has
    to be added with ALTER TABLE rather than by editing the CREATE TABLE text. Idempotent and safe
    to run on every startup: existing rows (including the live active persona) backfill to
    DEFAULT_MODEL, and a second run is a no-op because the column is already present.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(persona_prompt)")}
    if "model" not in columns:
        # DEFAULT_MODEL is a trusted in-code constant, not user input, so interpolating it into
        # the DDL is safe (ALTER TABLE ... ADD COLUMN requires a literal default anyway).
        conn.execute(
            f"ALTER TABLE persona_prompt ADD COLUMN model TEXT NOT NULL DEFAULT '{DEFAULT_MODEL}'"
        )


def migrate_chat_session(conn: sqlite3.Connection) -> None:
    """Add chat_message.session_id for the multi-session general chat feature.

    Same idempotent pattern as migrate_persona_prompt_model. The FIRST time this runs against a
    database that predates the column, it also discards the OLD single-thread general chat
    history: multi-session sessions replace that design entirely, and the owner explicitly chose
    not to migrate the old thread into a "session 1" (see the design spec). Entry-scoped chat
    rows (thread_kind='entry') are never touched by the DELETE. The index creation runs
    unconditionally (CREATE INDEX IF NOT EXISTS), since by this point the column is guaranteed to
    exist whether it was just added or already present.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(chat_message)")}
    if "session_id" not in columns:
        conn.execute(
            "ALTER TABLE chat_message ADD COLUMN session_id INTEGER REFERENCES chat_session(id)"
        )
        conn.execute("DELETE FROM chat_message WHERE thread_kind = 'general'")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_chat_message_session ON chat_message (session_id, created_at)"
    )


def create_chat_session(conn: sqlite3.Connection, title: str) -> int:
    cur = conn.execute(
        "INSERT INTO chat_session (title, created_at, updated_at) VALUES (?, ?, ?)",
        (title, _now(), _now()),
    )
    return cur.lastrowid


def list_chat_sessions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM chat_session ORDER BY updated_at DESC").fetchall()


def get_chat_session(conn: sqlite3.Connection, session_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM chat_session WHERE id = ?", (session_id,)).fetchone()


def rename_chat_session(conn: sqlite3.Connection, session_id: int, title: str) -> None:
    conn.execute(
        "UPDATE chat_session SET title = ?, updated_at = ? WHERE id = ?",
        (title, _now(), session_id),
    )


def touch_chat_session(conn: sqlite3.Connection, session_id: int) -> None:
    conn.execute("UPDATE chat_session SET updated_at = ? WHERE id = ?", (_now(), session_id))


def delete_chat_session(conn: sqlite3.Connection, session_id: int) -> None:
    """Delete a session and its messages atomically. No ON DELETE CASCADE in this schema (see
    Global Constraints) — the cascade is explicit, matching complete_job_item's manual-transaction
    pattern."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM chat_message WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM chat_session WHERE id = ?", (session_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_active_prompt(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM persona_prompt WHERE is_active = 1"
    ).fetchone()


def set_active_prompt(conn: sqlite3.Connection, body_text: str, model: str) -> int:
    """Insert a new persona_prompt version and make it the only active one. Atomic.

    model is required (never defaulted here): the model is part of "how generation happens" and
    every caller — the workshop apply route, the default-persona seeder — must choose it
    explicitly so a version never silently inherits some other version's model."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT MAX(version_no) AS m FROM persona_prompt").fetchone()
        next_version = (row["m"] or 0) + 1
        conn.execute("UPDATE persona_prompt SET is_active = 0 WHERE is_active = 1")
        cur = conn.execute(
            "INSERT INTO persona_prompt (version_no, body_text, model, is_active, created_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (next_version, body_text, model, _now()),
        )
        new_id = cur.lastrowid
        conn.execute("COMMIT")
        return new_id
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_current_commentary(conn: sqlite3.Connection, entry_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM entry_commentary WHERE entry_id = ? AND status = 'ok' "
        "ORDER BY created_at DESC LIMIT 1",
        (entry_id,),
    ).fetchone()


def get_current_report(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM aggregate_report WHERE status = 'ok' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()


def list_commentary_versions(conn: sqlite3.Connection, entry_id: int) -> list[sqlite3.Row]:
    # Unlike get_current_commentary, the history view keeps failed rows too, so the owner can
    # see that a regen failed on a given date. Newest first.
    return conn.execute(
        "SELECT * FROM entry_commentary WHERE entry_id = ? ORDER BY created_at DESC",
        (entry_id,),
    ).fetchall()


def get_commentary_by_id(conn: sqlite3.Connection, commentary_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM entry_commentary WHERE id = ?", (commentary_id,)
    ).fetchone()


def list_report_versions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM aggregate_report ORDER BY created_at DESC").fetchall()


def get_report_by_id(conn: sqlite3.Connection, report_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM aggregate_report WHERE id = ?", (report_id,)).fetchone()


def start_regen_job(conn: sqlite3.Connection, prompt_version_id: int, entry_ids: list[int]) -> int:
    """Create a regen_job + one regen_job_item per entry + one for the aggregate report.
    Raises sqlite3.IntegrityError (via the partial unique index) if a job is already running."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO regen_job (prompt_version_id, status, created_at, started_at) "
            "VALUES (?, 'running', ?, ?)",
            (prompt_version_id, _now(), _now()),
        )
        job_id = cur.lastrowid
        for entry_id in entry_ids:
            conn.execute(
                "INSERT INTO regen_job_item (job_id, target_type, entry_id, status, updated_at) "
                "VALUES (?, 'entry_commentary', ?, 'pending', ?)",
                (job_id, entry_id, _now()),
            )
        conn.execute(
            "INSERT INTO regen_job_item (job_id, target_type, entry_id, status, updated_at) "
            "VALUES (?, 'aggregate_report', NULL, 'pending', ?)",
            (job_id, _now()),
        )
        conn.execute("COMMIT")
        return job_id
    except Exception:
        conn.execute("ROLLBACK")
        raise


def complete_job_item(conn: sqlite3.Connection, item_id: int, result_table: str, result_row: dict) -> None:
    """Insert the generated commentary/report row and mark the job item 'ok' atomically.
    result_table must be 'entry_commentary' or 'aggregate_report'."""
    if result_table not in ("entry_commentary", "aggregate_report"):
        raise ValueError(f"invalid result_table: {result_table}")
    columns = ", ".join(result_row.keys())
    placeholders = ", ".join("?" for _ in result_row)
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            f"INSERT INTO {result_table} ({columns}) VALUES ({placeholders})",
            tuple(result_row.values()),
        )
        result_id = cur.lastrowid
        conn.execute(
            "UPDATE regen_job_item SET status = 'ok', result_id = ?, updated_at = ? WHERE id = ?",
            (result_id, _now(), item_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def fail_job_item(conn: sqlite3.Connection, item_id: int, error: str) -> None:
    conn.execute(
        "UPDATE regen_job_item SET status = 'failed', error = ?, attempts = attempts + 1, "
        "updated_at = ? WHERE id = ?",
        (error, _now(), item_id),
    )


def resume_sweep(conn: sqlite3.Connection) -> int:
    """Startup recovery: any regen_job_item left 'running' from a hard crash goes back to
    'pending' so the worker picks it up again. Safe because complete_job_item() is atomic —
    a 'running' item never has a result row already written. Returns the count reset."""
    cur = conn.execute(
        "UPDATE regen_job_item SET status = 'pending', updated_at = ? WHERE status = 'running'",
        (_now(),),
    )
    return cur.rowcount
