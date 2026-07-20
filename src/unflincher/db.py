"""SQLite (WAL) schema and the core query helpers every route/worker task builds on.

Design invariants enforced here (see plan Global Constraints):
- Only one persona_prompt row may have is_active=1 at a time (partial unique index).
- Only one regen_job may be status='running' at a time (partial unique index).
- "Current" commentary/report = latest row WHERE status='ok' (never plain latest-by-date).
- complete_job_item() writes the result row and flips the job item to 'ok' in ONE transaction.
- entry_commentary keeps only its latest row per entry_id (older rows are pruned the moment a new
  one is written); aggregate_report keeps its full version history unchanged. See
  complete_job_item() and migrate_prune_entry_commentary_history().

Generation-safety invariants added alongside the maintenance gate / lease / snapshot foundations
(see the plan's Maintenance gate and Context budget and failure contract sections):
- generation_lease.target_key is UNIQUE: at most one active lease may ever exist per Entry
  Reflection/Life Report target or conversation thread, system-wide.
- acquire_lease() checks the maintenance flag and inserts the lease row in ONE BEGIN IMMEDIATE
  transaction, so a lease can never be granted in the instant after maintenance locks.
- enqueue_snapshot_regen_job() atomically compares the current ordered archive against the
  caller's preflight snapshot, acquires every target's lease, and writes the job/items/snapshot
  rows -- all or nothing, under one BEGIN IMMEDIATE.
- retry_failed_job_item() is the only way to requeue a failed item, and only when its persisted
  baseline_result_id still equals the target's current result and no newer same-target item
  exists.
- recover_or_cancel_running_jobs() is the ONLY path that may resume a 'running' job after a
  crash, and only when that job has a stored context snapshot; a snapshot-less legacy job is
  always cancelled, never resumed against the live archive.
"""
import hashlib
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

# Fallback model for a brand-new install and for rows that predate the persona_prompt.model
# column (see migrate_persona_prompt_model). Kept in sync with config.py's UNFLINCHER_LLM_MODEL
# default so upgrading an existing deployment leaves generation behaviour unchanged.
DEFAULT_MODEL = "claude-sonnet-4.6"

V01_EFFECTIVE_SCHEMA = """
CREATE TABLE diary_entry (
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
CREATE TABLE persona_prompt (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_no INTEGER NOT NULL,
    body_text TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    model TEXT NOT NULL DEFAULT 'claude-sonnet-4.6'
);
CREATE UNIQUE INDEX ux_persona_prompt_one_active
    ON persona_prompt (is_active) WHERE is_active = 1;
CREATE TABLE entry_commentary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL REFERENCES diary_entry(id),
    prompt_version_id INTEGER NOT NULL REFERENCES persona_prompt(id),
    model TEXT NOT NULL,
    body_text TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_entry_commentary_entry_id
    ON entry_commentary (entry_id, created_at DESC);
CREATE TABLE aggregate_report (
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
CREATE TABLE chat_session (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE chat_message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_kind TEXT NOT NULL CHECK (thread_kind IN ('entry', 'general')),
    entry_id INTEGER REFERENCES diary_entry(id),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    model TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    session_id INTEGER REFERENCES chat_session(id)
);
CREATE INDEX ix_chat_message_thread
    ON chat_message (thread_kind, entry_id, created_at);
CREATE INDEX ix_chat_message_session
    ON chat_message (session_id, created_at);
CREATE TABLE regen_job (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_version_id INTEGER NOT NULL REFERENCES persona_prompt(id),
    status TEXT NOT NULL CHECK (status IN ('running', 'done', 'cancelled')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT
);
CREATE UNIQUE INDEX ux_regen_job_one_running
    ON regen_job (status) WHERE status = 'running';
CREATE TABLE regen_job_item (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES regen_job(id),
    target_type TEXT NOT NULL CHECK (target_type IN ('entry_commentary', 'aggregate_report')),
    entry_id INTEGER REFERENCES diary_entry(id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'ok', 'failed'))
        DEFAULT 'pending',
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    result_id INTEGER,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_regen_job_item_job_id
    ON regen_job_item (job_id, status);
"""

V01_RELEASE_SCHEMA = (
    V01_EFFECTIVE_SCHEMA.replace(
        """    is_active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    model TEXT NOT NULL DEFAULT 'claude-sonnet-4.6'
);""",
        """    is_active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);""",
    )
    .replace(
        """    model TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    session_id INTEGER REFERENCES chat_session(id)
);""",
        """    model TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);""",
    )
    .replace(
        """CREATE INDEX ix_chat_message_session
    ON chat_message (session_id, created_at);
""",
        "",
    )
)

_REQUIRED_CHECK_FRAGMENTS = {
    "diary_entry": ("CHECK (source IN ('import', 'manual'))",),
    "entry_commentary": ("CHECK (status IN ('ok', 'failed'))",),
    "aggregate_report": ("CHECK (status IN ('ok', 'failed'))",),
    "chat_message": (
        "CHECK (thread_kind IN ('entry', 'general'))",
        "CHECK (role IN ('user', 'assistant'))",
    ),
    "regen_job": ("CHECK (status IN ('running', 'done', 'cancelled'))",),
    "regen_job_item": (
        "CHECK (target_type IN ('entry_commentary', 'aggregate_report'))",
        "CHECK (status IN ('pending', 'running', 'ok', 'failed'))",
    ),
    "maintenance_control": ("CHECK (id = 1)",),
    "generation_lease": (
        "CHECK (lease_kind IN ('direct', 'background', 'thread', 'request'))",
    ),
    "db_bootstrap_state": ("CHECK (id = 1)",),
}

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
    preset_key TEXT,
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

-- Generation-safety foundations (maintenance gate, exclusive leases, ordered archive snapshots).
-- Additive/new tables only; brand-new databases get these via CREATE TABLE IF NOT EXISTS here,
-- existing databases get them the same way (they don't exist yet on an old DB either). Additive
-- COLUMNS on the tables above (regen_job.snapshot_entry_count,
-- regen_job_item.request_format_version/request_fingerprint/baseline_result_id) are handled by
-- migrate_generation_safety() below, following the same ALTER-TABLE pattern as
-- migrate_persona_prompt_model/migrate_chat_session (a new database also needs that function run
-- once, exactly like those two).

-- Single-row flag: while locked, no NEW generation work may be admitted (see acquire_lease()).
-- Exactly two bypasses exist in the eventual design: recovery of a job already admitted before
-- maintenance began, and the local synthetic non-persisting deployment probe (see probe.py) --
-- neither goes through acquire_lease()'s maintenance check.
CREATE TABLE IF NOT EXISTS maintenance_control (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    locked INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO maintenance_control (id, locked) VALUES (1, 0);

-- One row per active direct generation, background target, or conversation thread/request. The
-- UNIQUE target_key is what makes two generations for the SAME Entry Reflection, Life Report, or
-- conversation thread mutually exclusive -- see acquire_lease().
CREATE TABLE IF NOT EXISTS generation_lease (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_key TEXT NOT NULL UNIQUE,
    lease_kind TEXT NOT NULL CHECK (lease_kind IN ('direct', 'background', 'thread', 'request')),
    owner_token TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The ordered Journal Archive membership captured at enqueue time for one regen_job, stored as
-- entry IDs plus an EXPLICIT ordinal -- workers, retries, and recovery read rows in ordinal
-- order, never live-archive or SQL `IN` order (which SQLite never guarantees). The canonical
-- order is (entry_date ASC, id ASC); see get_ordered_entry_ids().
CREATE TABLE IF NOT EXISTS regen_job_entry_snapshot (
    job_id INTEGER NOT NULL REFERENCES regen_job(id),
    ordinal INTEGER NOT NULL,
    entry_id INTEGER NOT NULL REFERENCES diary_entry(id),
    PRIMARY KEY (job_id, ordinal)
);
CREATE INDEX IF NOT EXISTS ix_regen_job_entry_snapshot_job ON regen_job_entry_snapshot (job_id);
"""


def _configure_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    # check_same_thread=False: the app uses one connection stored on app.state, shared across
    # async route handlers and the background worker task, all of which run on the single
    # uvicorn --workers 1 event-loop thread (see Global Constraints) — never a real multi-thread
    # writer scenario, this just avoids sqlite3's default same-thread assertion tripping in tests.
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def enable_wal_mode(conn: sqlite3.Connection) -> None:
    """Persist WAL mode only after the caller has passed safety checks."""
    conn.execute("PRAGMA journal_mode=WAL")


def get_connection(db_path: str) -> sqlite3.Connection:
    return _configure_connection(
        sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    )


def get_existing_connection(db_path: str) -> sqlite3.Connection:
    """Open an existing database atomically; SQLite must never create a typo/raced-away path."""
    database_uri = f"{Path(db_path).resolve().as_uri()}?mode=rw"
    try:
        return _configure_connection(
            sqlite3.connect(
                database_uri,
                uri=True,
                isolation_level=None,
                check_same_thread=False,
            )
        )
    except sqlite3.OperationalError as exc:
        raise RuntimeError(f"cannot open existing database: {db_path}") from exc


def _normalize_schema_sql(value: str | None) -> str:
    if value is None:
        return ""
    parts = re.split(r"('(?:''|[^'])*')", value.strip())
    for index in range(0, len(parts), 2):
        parts[index] = re.sub(r"\s+", " ", parts[index])
        parts[index] = re.sub(r"\s*([(),])\s*", r"\1", parts[index])
    return "".join(parts)


def _table_contract(conn: sqlite3.Connection, table_name: str) -> dict[str, object]:
    columns = {
        row["name"]: (
            row["type"].upper(),
            row["notnull"],
            row["dflt_value"],
            row["pk"],
        )
        for row in conn.execute(f"PRAGMA table_info({table_name})")
    }
    foreign_keys = sorted(
        (
            row["from"],
            row["table"],
            row["to"],
            row["on_update"],
            row["on_delete"],
            row["match"],
        )
        for row in conn.execute(f"PRAGMA foreign_key_list({table_name})")
    )
    indexes = []
    for row in conn.execute(f"PRAGMA index_list({table_name})"):
        index_name = row["name"]
        index_columns = tuple(
            item["name"] for item in conn.execute(f"PRAGMA index_info({index_name})")
        )
        sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (index_name,),
        ).fetchone()
        index_sql = _normalize_schema_sql(sql_row["sql"] if sql_row else None)
        indexes.append(
            (
                index_name if index_sql else "",
                row["unique"],
                row["origin"],
                row["partial"],
                index_columns,
                index_sql,
            )
        )
    table_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return {
        "columns": columns,
        "foreign_keys": foreign_keys,
        "indexes": sorted(indexes),
        "sql": _normalize_schema_sql(table_row["sql"] if table_row else None),
    }


def _database_schema_contract(conn: sqlite3.Connection) -> dict[str, dict[str, object]]:
    table_names = sorted(
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    )
    return {table_name: _table_contract(conn, table_name) for table_name in table_names}


@lru_cache(maxsize=1)
def _expected_v01_schema_contract() -> dict[str, dict[str, object]]:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(V01_EFFECTIVE_SCHEMA)
        return _database_schema_contract(conn)
    finally:
        conn.close()


@lru_cache(maxsize=1)
def _expected_v02_schema_contract() -> dict[str, dict[str, object]]:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        migrate_bootstrap_state(conn)
        init_schema(conn)
        migrate_persona_prompt_model(conn)
        migrate_persona_prompt_preset_key(conn)
        migrate_chat_session(conn)
        migrate_generation_safety(conn)
        return _database_schema_contract(conn)
    finally:
        conn.close()


def _capture_table_sql_variants(
    variants: dict[str, set[str]],
    conn: sqlite3.Connection,
) -> None:
    for table_name, contract in _database_schema_contract(conn).items():
        variants.setdefault(table_name, set()).add(contract["sql"])


@lru_cache(maxsize=1)
def _expected_table_sql_variants() -> dict[str, frozenset[str]]:
    variants: dict[str, set[str]] = {}

    effective = sqlite3.connect(":memory:", isolation_level=None)
    effective.row_factory = sqlite3.Row
    try:
        effective.execute("PRAGMA foreign_keys=ON")
        effective.executescript(V01_EFFECTIVE_SCHEMA)
        _capture_table_sql_variants(variants, effective)
    finally:
        effective.close()

    upgraded = sqlite3.connect(":memory:", isolation_level=None)
    upgraded.row_factory = sqlite3.Row
    try:
        upgraded.execute("PRAGMA foreign_keys=ON")
        upgraded.executescript(V01_RELEASE_SCHEMA)
        migrate_persona_prompt_model(upgraded)
        migrate_chat_session(upgraded)
        _capture_table_sql_variants(variants, upgraded)
        migrate_bootstrap_state(upgraded)
        init_schema(upgraded)
        _capture_table_sql_variants(variants, upgraded)
        migrate_persona_prompt_preset_key(upgraded)
        _capture_table_sql_variants(variants, upgraded)
        upgraded.execute("ALTER TABLE regen_job ADD COLUMN snapshot_entry_count INTEGER")
        _capture_table_sql_variants(variants, upgraded)
        for statement in (
            "ALTER TABLE regen_job_item ADD COLUMN request_format_version INTEGER",
            "ALTER TABLE regen_job_item ADD COLUMN request_fingerprint TEXT",
            "ALTER TABLE regen_job_item ADD COLUMN baseline_result_id INTEGER",
        ):
            upgraded.execute(statement)
            _capture_table_sql_variants(variants, upgraded)
    finally:
        upgraded.close()

    fresh = sqlite3.connect(":memory:", isolation_level=None)
    fresh.row_factory = sqlite3.Row
    try:
        fresh.execute("PRAGMA foreign_keys=ON")
        migrate_bootstrap_state(fresh)
        init_schema(fresh)
        migrate_persona_prompt_model(fresh)
        migrate_persona_prompt_preset_key(fresh)
        migrate_chat_session(fresh)
        migrate_generation_safety(fresh)
        _capture_table_sql_variants(variants, fresh)
    finally:
        fresh.close()

    return {
        table_name: frozenset(sql_values)
        for table_name, sql_values in variants.items()
    }


def verify_v01_upgrade_schema(
    conn: sqlite3.Connection,
    *,
    require_v02: bool = False,
) -> None:
    """Validate the frozen v0.1 contract and only known additive v0.2 schema changes."""
    actual = _database_schema_contract(conn)
    expected_v01 = _expected_v01_schema_contract()
    expected_v02 = _expected_v02_schema_contract()
    expected_sql = _expected_table_sql_variants()
    actual_tables = set(actual)
    v01_tables = set(expected_v01)
    v02_tables = set(expected_v02)
    missing_tables = sorted(v01_tables - actual_tables)
    unknown_tables = sorted(actual_tables - v02_tables)
    if require_v02:
        missing_tables.extend(sorted(v02_tables - actual_tables))

    problems = []
    if missing_tables:
        problems.append(f"missing_tables={sorted(set(missing_tables))}")
    if unknown_tables:
        problems.append(f"unknown_tables={unknown_tables}")
    unexpected_objects = [
        f"{row['type']}:{row['name']}"
        for row in conn.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE type IN ('trigger', 'view') ORDER BY type, name"
        )
    ]
    if unexpected_objects:
        problems.append(f"unexpected_objects={unexpected_objects}")

    for table_name in sorted(actual_tables.intersection(v02_tables)):
        actual_table = actual[table_name]
        expected_current = expected_v02[table_name]
        if table_name in expected_v01:
            expected_base = expected_v01[table_name]
            base_columns = expected_base["columns"]
            current_columns = expected_current["columns"]
            actual_columns = actual_table["columns"]
            missing_columns = sorted(set(base_columns) - set(actual_columns))
            unknown_columns = sorted(set(actual_columns) - set(current_columns))
            wrong_columns = sorted(
                column
                for column in set(actual_columns).intersection(base_columns)
                if actual_columns[column] != base_columns[column]
            )
            wrong_additions = sorted(
                column
                for column in set(actual_columns) - set(base_columns)
                if column in current_columns
                and actual_columns[column] != current_columns[column]
            )
            if require_v02:
                missing_columns.extend(
                    sorted(set(current_columns) - set(actual_columns))
                )
            if missing_columns or unknown_columns or wrong_columns or wrong_additions:
                problems.append(
                    f"{table_name}.columns="
                    f"missing:{sorted(set(missing_columns))},"
                    f"unknown:{unknown_columns},"
                    f"wrong:{wrong_columns + wrong_additions}"
                )
        elif actual_table["columns"] != expected_current["columns"]:
            problems.append(f"{table_name}.columns")

        if actual_table["foreign_keys"] != expected_current["foreign_keys"]:
            problems.append(f"{table_name}.foreign_keys")
        indexes_match = actual_table["indexes"] == expected_current["indexes"]
        if (
            not indexes_match
            and not require_v02
            and table_name == "regen_job_entry_snapshot"
        ):
            expected_before_named_index = [
                index
                for index in expected_current["indexes"]
                if index[0] != "ix_regen_job_entry_snapshot_job"
            ]
            indexes_match = actual_table["indexes"] == expected_before_named_index
        if not indexes_match:
            problems.append(f"{table_name}.indexes")
        if actual_table["sql"] not in expected_sql.get(table_name, frozenset()):
            problems.append(f"{table_name}.sql")
        for fragment in _REQUIRED_CHECK_FRAGMENTS.get(table_name, ()):
            if _normalize_schema_sql(fragment) not in actual_table["sql"]:
                problems.append(f"{table_name}.check:{fragment}")

    if problems:
        raise RuntimeError(
            "database does not match the released v0.1/v0.2 schema contract: "
            + "; ".join(problems)
        )


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


def migrate_persona_prompt_preset_key(conn: sqlite3.Connection) -> None:
    """Add persona_prompt.preset_key to a database created before the column existed (see the
    plan's Persistence and migration section).

    Unlike migrate_persona_prompt_model, brand-new databases ALSO get this column straight from
    SCHEMA's own CREATE TABLE (see item 1's explicit "new-database schema and an idempotent
    migration for existing DBs" requirement) -- this function exists purely for a database that
    was created by an EARLIER release, before the column existed. Idempotent and safe on every
    startup. preset_key is nullable with NO default, so ALTER TABLE ADD COLUMN backfills every
    pre-existing row to NULL (Custom) automatically -- no body text, model, active state, version
    number, or created_at is ever touched by this migration."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(persona_prompt)")}
    if "preset_key" not in columns:
        conn.execute("ALTER TABLE persona_prompt ADD COLUMN preset_key TEXT")


def migrate_bootstrap_state(conn: sqlite3.Connection) -> None:
    """One-time, crash-safe fresh-vs-upgrade classification. MUST run before init_schema() so it
    can observe the database's tables beforehand -- see initialize_database, the one entry point
    app startup and the CLI import bootstrap both use.

    "Fresh" means NO pre-existing user tables at all, not merely "persona_prompt is absent" -- a
    pre-v0.2 database that crashed partway through init_schema() (e.g. diary_entry exists but
    persona_prompt does not yet) is a partial schema, not a truly new install, and must never be
    seeded. Row count is not a safe freshness signal either way (an upgrade database can
    legitimately have zero persona_prompt rows).

    For a genuine upgrade, verify_current_result_selection_compatibility runs read-only before
    any mutation -- a tie or mismatch propagates before anything is ever written. Records
    is_fresh_install, analyst_seeded (0 until seed_analyst_prompt_if_fresh_install actually
    inserts a row), and current_result_selection_verified.

    The durable marker is read explicitly, not inferred from mere table existence: a missing
    singleton row is an explicit error (never silently treated as verified); verified=1 is a
    no-op; verified=0 reruns the read-only verifier and only then atomically sets it to 1 --
    on failure the flag stays 0 and nothing else changes."""
    existing_tables = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }

    if "db_bootstrap_state" in existing_tables:
        row = conn.execute("SELECT * FROM db_bootstrap_state WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("db_bootstrap_state exists but its singleton row (id=1) is missing")
        if row["current_result_selection_verified"]:
            return
        verify_current_result_selection_compatibility(conn)  # read-only; raises before any write
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("UPDATE db_bootstrap_state SET current_result_selection_verified = 1 WHERE id = 1")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return

    # sqlite_sequence is SQLite's own AUTOINCREMENT bookkeeping table, not a user table -- a
    # database with only that (or nothing) has no schema of its own yet.
    was_pre_existing = bool(existing_tables - {"sqlite_sequence"})
    if was_pre_existing:
        verify_current_result_selection_compatibility(conn)  # read-only; raises before any write

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "CREATE TABLE db_bootstrap_state ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), "
            "is_fresh_install INTEGER NOT NULL, "
            "analyst_seeded INTEGER NOT NULL DEFAULT 0, "
            "current_result_selection_verified INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        is_fresh = 0 if was_pre_existing else 1
        conn.execute(
            "INSERT INTO db_bootstrap_state "
            "(id, is_fresh_install, analyst_seeded, current_result_selection_verified) "
            "VALUES (1, ?, ?, 1)",
            (is_fresh, 0 if is_fresh else 1),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


class OfflineBootstrapRequiredError(RuntimeError):
    """A pre-v0.2 database must be migrated by the fail-locked offline CLI first."""


def get_bootstrap_state(conn: sqlite3.Connection) -> dict[str, bool] | None:
    tables = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "db_bootstrap_state" not in tables:
        return None
    rows = conn.execute(
        "SELECT id, is_fresh_install, analyst_seeded, current_result_selection_verified "
        "FROM db_bootstrap_state ORDER BY id"
    ).fetchall()
    if len(rows) != 1 or rows[0]["id"] != 1:
        raise RuntimeError("db_bootstrap_state must contain exactly the singleton row id=1")
    row = rows[0]
    for field in (
        "is_fresh_install",
        "analyst_seeded",
        "current_result_selection_verified",
    ):
        if row[field] not in (0, 1):
            raise RuntimeError(f"db_bootstrap_state.{field} must be 0 or 1")
    return {
        "is_fresh_install": bool(row["is_fresh_install"]),
        "analyst_seeded": bool(row["analyst_seeded"]),
        "current_result_selection_verified": bool(
            row["current_result_selection_verified"]
        ),
    }


def _migrate_database_schema(conn: sqlite3.Connection) -> None:
    migrate_bootstrap_state(conn)
    init_schema(conn)
    migrate_persona_prompt_model(conn)
    migrate_persona_prompt_preset_key(conn)
    migrate_chat_session(conn)
    migrate_generation_safety(conn)
    migrate_prune_entry_commentary_history(conn)


def initialize_database(conn: sqlite3.Connection) -> None:
    """Initialize fresh/v0.2 databases, but refuse a v0.1 database that skipped offline bootstrap."""
    tables = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    bootstrap_state = get_bootstrap_state(conn)
    application_tables = set(_expected_v01_schema_contract())
    if bootstrap_state is None and tables.intersection(application_tables):
        raise OfflineBootstrapRequiredError(
            "offline bootstrap required before starting v0.2 against a pre-existing database"
        )
    if bootstrap_state is None and tables - {"sqlite_sequence"}:
        raise RuntimeError("database contains unrecognized pre-existing tables")
    if (
        bootstrap_state is not None
        and bootstrap_state["is_fresh_install"]
        and not bootstrap_state["analyst_seeded"]
    ):
        enable_wal_mode(conn)
        _migrate_database_schema(conn)
        seed_analyst_prompt_if_fresh_install(conn)
        require_v02_operational_schema(conn)
        return
    if bootstrap_state is not None:
        require_v02_operational_schema(conn)
        enable_wal_mode(conn)
        return
    enable_wal_mode(conn)
    _migrate_database_schema(conn)
    seed_analyst_prompt_if_fresh_install(conn)


def initialize_upgrade_database(conn: sqlite3.Connection) -> None:
    """Run the additive v0.1-to-v0.2 schema path without any prompt seeding."""
    _migrate_database_schema(conn)


def require_v02_operational_schema(conn: sqlite3.Connection) -> dict[str, bool]:
    """Require a completed, verified v0.2 database before live maintenance operations."""
    state = get_bootstrap_state(conn)
    if state is None:
        raise RuntimeError("offline bootstrap has not completed")
    verify_v01_upgrade_schema(conn, require_v02=True)
    get_maintenance_locked(conn)
    if not state["analyst_seeded"]:
        raise RuntimeError("db_bootstrap_state.analyst_seeded is not complete")
    if not state["current_result_selection_verified"]:
        raise RuntimeError(
            "db_bootstrap_state.current_result_selection_verified is not complete"
        )
    return state


def seed_analyst_prompt_if_fresh_install(conn: sqlite3.Connection) -> None:
    """Seeds exactly one active persona_prompt (the Analyst preset, preset_key='analyst',
    version 1, DEFAULT_MODEL) the first time it is safe on a genuinely fresh database -- see
    migrate_bootstrap_state (which MUST already have run). A no-op otherwise: an upgraded
    database (analyst_seeded forced to 1), an already-seeded fresh database, or a missing
    bootstrap-state row (caller bug, never treated as "seed anyway"). Insert and flag-update
    happen in one transaction, so a crash between them is impossible."""
    from unflincher.perspectives import DEFAULT_PERSPECTIVE_KEY, get_preset

    conn.execute("BEGIN IMMEDIATE")
    try:
        tables = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "db_bootstrap_state" not in tables:
            conn.execute("COMMIT")
            return
        row = conn.execute("SELECT * FROM db_bootstrap_state WHERE id = 1").fetchone()
        if row is None or row["analyst_seeded"]:
            conn.execute("COMMIT")
            return
        preset = get_preset(DEFAULT_PERSPECTIVE_KEY)
        version_row = conn.execute("SELECT MAX(version_no) AS m FROM persona_prompt").fetchone()
        next_version = (version_row["m"] or 0) + 1
        conn.execute("UPDATE persona_prompt SET is_active = 0 WHERE is_active = 1")
        conn.execute(
            "INSERT INTO persona_prompt (version_no, body_text, model, preset_key, is_active, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (next_version, preset.prompt, DEFAULT_MODEL, preset.key, _now()),
        )
        conn.execute("UPDATE db_bootstrap_state SET analyst_seeded = 1 WHERE id = 1")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


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


def delete_chat_session(conn: sqlite3.Connection, session_id: int, owner_token: str) -> None:
    """Delete a session and its messages atomically. No ON DELETE CASCADE in this schema (see
    Global Constraints) — the cascade is explicit, matching complete_job_item's manual-transaction
    pattern.

    Follows the plan literally: in the SAME BEGIN IMMEDIATE transaction, ACQUIRE the session's
    thread lease (conversation:<session_id>) -- not merely check for one -- delete the messages
    and session, then release that same deletion lease, then commit. Acquiring (rather than just
    checking) reuses the exact same UNIQUE-target_key exclusivity every other lease-guarded path
    relies on: if an active turn already holds this lease, the INSERT here trips the same
    constraint and raises TargetBusyError (no write; the session is preserved), so a concurrent
    stream can never be deleted out from under it. Deletion is deliberately NOT gated by the
    maintenance flag (it is cleanup, not new generation work) -- bypasses
    get_maintenance_locked() entirely, unlike acquire_lease()."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        target_key = conversation_thread_key(session_id)
        try:
            conn.execute(
                "INSERT INTO generation_lease (target_key, lease_kind, owner_token, created_at) "
                "VALUES (?, 'thread', ?, ?)",
                (target_key, owner_token, _now()),
            )
        except sqlite3.IntegrityError as exc:
            raise TargetBusyError(target_key) from exc
        conn.execute("DELETE FROM chat_message WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM chat_session WHERE id = ?", (session_id,))
        # Release the deletion lease we just acquired -- its target no longer exists, and this
        # commits together with the delete: either both happen, or neither does.
        conn.execute("DELETE FROM generation_lease WHERE target_key = ?", (target_key,))
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


ACTIVE_PROMPT_MANIFEST_FIELDS = (
    "id",
    "version_no",
    "body_sha256",
    "model",
    "is_active",
    "created_at",
    "preset_key",
)


def prompt_identity_manifest(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Return every prompt's identity without exposing any private body text."""
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "persona_prompt" not in tables:
        return []

    columns = {row[1] for row in conn.execute("PRAGMA table_info(persona_prompt)")}
    required_columns = ("id", "version_no", "body_text", "is_active", "created_at")
    missing = set(required_columns) - columns
    if missing:
        raise RuntimeError(f"persona_prompt is missing required column(s): {sorted(missing)}")

    optional_columns = [name for name in ("model", "preset_key") if name in columns]
    select_columns = [*required_columns, *optional_columns]
    rows = conn.execute(
        f"SELECT {', '.join(select_columns)} FROM persona_prompt ORDER BY id"
    ).fetchall()
    manifest = []
    for row in rows:
        values = dict(zip(select_columns, row))
        body_text = values["body_text"]
        if not isinstance(body_text, str):
            raise RuntimeError(
                f"persona_prompt.body_text is not TEXT (got {type(body_text).__name__})"
            )
        manifest.append(
            {
                "id": values["id"],
                "version_no": values["version_no"],
                "body_sha256": hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
                "model": values.get("model"),
                "is_active": values["is_active"],
                "created_at": values["created_at"],
                "preset_key": values.get("preset_key"),
            }
        )
    return manifest


def active_prompt_manifest(conn: sqlite3.Connection) -> dict[str, object] | None:
    """Return the active prompt's identity without exposing its private body text."""
    active = [row for row in prompt_identity_manifest(conn) if row["is_active"] == 1]
    if not active:
        return None
    if len(active) > 1:
        raise RuntimeError(f"persona_prompt has {len(active)} active rows; expected at most one")
    return active[0]


def install_prompt_identity_guard(conn: sqlite3.Connection) -> None:
    """Block prompt DML on this connection while the offline upgrade runs."""
    for operation in ("INSERT", "UPDATE", "DELETE"):
        trigger_name = f"unflincher_bootstrap_block_prompt_{operation.lower()}"
        conn.execute(
            f"CREATE TEMP TRIGGER {trigger_name} "
            f"BEFORE {operation} ON persona_prompt "
            "BEGIN SELECT RAISE(ABORT, 'persona_prompt is immutable during bootstrap'); END"
        )


def remove_prompt_identity_guard(conn: sqlite3.Connection) -> None:
    for operation in ("insert", "update", "delete"):
        conn.execute(
            f"DROP TRIGGER IF EXISTS unflincher_bootstrap_block_prompt_{operation}"
        )


def _insert_activated_persona_prompt_version(
    conn: sqlite3.Connection, body_text: str, model: str,
) -> int:
    """The ONE place a new persona_prompt version is created and made active -- used by
    set_active_prompt, set_active_prompt_and_start_regen_job, and
    enqueue_snapshot_regen_job's activate_prompt path so all three cannot drift apart.

    preset_key always comes from perspectives.classify_prompt(body_text) -- an exact match
    against a current shipped preset, or NULL (Custom). Any caller-claimed preset hint is
    resolved here and only here; it is never trusted directly (see each public function's own
    `preset_key`/`activate_preset_key` parameter docstring). Caller owns the transaction."""
    from unflincher.perspectives import classify_prompt

    row = conn.execute("SELECT MAX(version_no) AS m FROM persona_prompt").fetchone()
    next_version = (row["m"] or 0) + 1
    conn.execute("UPDATE persona_prompt SET is_active = 0 WHERE is_active = 1")
    cur = conn.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, preset_key, is_active, created_at) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        (next_version, body_text, model, classify_prompt(body_text), _now()),
    )
    return cur.lastrowid


def set_active_prompt(
    conn: sqlite3.Connection, body_text: str, model: str, *, preset_key: str | None = None,
) -> int:
    """Insert a new persona_prompt version and make it the only active one. Atomic.

    model is required: every caller chooses it explicitly so a version never silently inherits
    another version's model.

    preset_key is an optional caller-claimed hint (unused today; accepted for a future Workshop
    UI). It is NEVER trusted: the stored value always comes from
    perspectives.classify_prompt(body_text) via _insert_activated_persona_prompt_version, so a
    stale or forged hint can never misclassify edited/arbitrary text, and an exact shipped preset
    classifies correctly even if the hint claims something else."""
    del preset_key  # server-side classification only -- see docstring
    conn.execute("BEGIN IMMEDIATE")
    try:
        new_id = _insert_activated_persona_prompt_version(conn, body_text, model)
        conn.execute("COMMIT")
        return new_id
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_current_commentary(conn: sqlite3.Connection, entry_id: int) -> sqlite3.Row | None:
    # "Current" is the highest successful RESULT ID, not latest created_at (see the plan's
    # Persistence and migration section) -- exclusive same-target leases serialize admission and
    # completion, so result IDs give a monotonic order independent of clock format/rollback. Safe
    # because verify_current_result_selection_compatibility() is a one-time compatibility
    # precondition (see migrate_bootstrap_state) that must pass before any pre-existing database
    # is ever allowed to reach this query.
    return conn.execute(
        "SELECT ec.*, pp.version_no AS prompt_version_no, pp.preset_key AS prompt_preset_key "
        "FROM entry_commentary ec JOIN persona_prompt pp ON pp.id = ec.prompt_version_id "
        "WHERE ec.entry_id = ? AND ec.status = 'ok' ORDER BY ec.id DESC LIMIT 1",
        (entry_id,),
    ).fetchone()


def get_current_report(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT ar.*, pp.version_no AS prompt_version_no, pp.preset_key AS prompt_preset_key "
        "FROM aggregate_report ar JOIN persona_prompt pp ON pp.id = ar.prompt_version_id "
        "WHERE ar.status = 'ok' ORDER BY ar.id DESC LIMIT 1"
    ).fetchone()


def list_report_versions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT ar.*, pp.version_no AS prompt_version_no, pp.preset_key AS prompt_preset_key "
        "FROM aggregate_report ar JOIN persona_prompt pp ON pp.id = ar.prompt_version_id "
        "ORDER BY ar.created_at DESC"
    ).fetchall()


def get_report_by_id(conn: sqlite3.Connection, report_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT ar.*, pp.version_no AS prompt_version_no, pp.preset_key AS prompt_preset_key "
        "FROM aggregate_report ar JOIN persona_prompt pp ON pp.id = ar.prompt_version_id "
        "WHERE ar.id = ?",
        (report_id,),
    ).fetchone()


def _insert_full_regen_job(
    conn: sqlite3.Connection,
    prompt_version_id: int,
    entry_ids: list[int],
) -> int:
    """Insert one running regen_job plus one item per entry and one aggregate-report item.

    No transaction control of its own: callers wrap it so the single-flight
    ux_regen_job_one_running partial unique index (which trips sqlite3.IntegrityError if a job is
    already running) rolls back whatever the caller staged in the SAME transaction -- for the
    combined apply-and-start path, that includes the freshly activated prompt version."""
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
    return job_id


def start_regen_job(conn: sqlite3.Connection, prompt_version_id: int, entry_ids: list[int]) -> int:
    """Create a full regeneration job under an already-active prompt.
    Raises sqlite3.IntegrityError (via the partial unique index) if a job is already running."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        job_id = _insert_full_regen_job(conn, prompt_version_id, entry_ids)
        conn.execute("COMMIT")
        return job_id
    except Exception:
        conn.execute("ROLLBACK")
        raise


def set_active_prompt_and_start_regen_job(
    conn: sqlite3.Connection,
    body_text: str,
    model: str,
    entry_ids: list[int],
    *,
    preset_key: str | None = None,
) -> tuple[int, int]:
    """Activate one prompt version and create its full regeneration job atomically.

    Mirrors set_active_prompt (see its docstring re preset_key). If a job is already running,
    _insert_full_regen_job trips the single-flight index and the ROLLBACK below discards the
    uncommitted prompt version too -- never persists a prompt/preset key whose job was rejected
    as busy."""
    del preset_key  # server-side classification only -- see set_active_prompt's docstring
    conn.execute("BEGIN IMMEDIATE")
    try:
        prompt_id = _insert_activated_persona_prompt_version(conn, body_text, model)
        job_id = _insert_full_regen_job(conn, prompt_id, entry_ids)
        conn.execute("COMMIT")
        return prompt_id, job_id
    except Exception:
        conn.execute("ROLLBACK")
        raise


def start_single_entry_commentary_job(conn: sqlite3.Connection, prompt_version_id: int, entry_id: int) -> int:
    """Create a regen_job with exactly ONE regen_job_item (target_type='entry_commentary',
    this single entry_id) -- unlike start_regen_job, no aggregate_report item is created, since
    a single-entry trigger must never also kick off a full report regeneration. Raises
    sqlite3.IntegrityError (via the same partial unique index start_regen_job relies on) if a
    job is already running -- callers get the identical single-flight guarantee for free."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO regen_job (prompt_version_id, status, created_at, started_at) "
            "VALUES (?, 'running', ?, ?)",
            (prompt_version_id, _now(), _now()),
        )
        job_id = cur.lastrowid
        conn.execute(
            "INSERT INTO regen_job_item (job_id, target_type, entry_id, status, updated_at) "
            "VALUES (?, 'entry_commentary', ?, 'pending', ?)",
            (job_id, entry_id, _now()),
        )
        conn.execute("COMMIT")
        return job_id
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_entries_with_active_commentary_job(conn: sqlite3.Connection) -> set[int]:
    """entry_ids with a pending/running entry_commentary job item right now -- used by the
    timeline to show a "点评中" badge. A single query regardless of how many entries exist."""
    rows = conn.execute(
        "SELECT entry_id FROM regen_job_item WHERE target_type = 'entry_commentary' "
        "AND status IN ('pending', 'running')"
    ).fetchall()
    return {r["entry_id"] for r in rows}


def get_latest_commentary_job_item(conn: sqlite3.Connection, entry_id: int) -> sqlite3.Row | None:
    """The most recent regen_job_item (any status) for this entry, or None if a commentary job
    has never been triggered for it. Used by the entry-detail page to pick which of the
    idle/busy/failed states to render."""
    return conn.execute(
        "SELECT * FROM regen_job_item WHERE target_type = 'entry_commentary' AND entry_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (entry_id,),
    ).fetchone()


def complete_job_item(conn: sqlite3.Connection, item_id: int, result_table: str, result_row: dict) -> None:
    """Insert the generated commentary/report row and mark the job item 'ok' atomically.
    result_table must be 'entry_commentary' or 'aggregate_report'.

    entry_commentary keeps only its latest row per entry -- once the new row is in, every OTHER
    entry_commentary row for the same entry_id is deleted in this SAME transaction. aggregate_report
    is never pruned; it keeps its full version history. Safe with respect to staleness/retry
    checks: those compare against the CURRENT (highest-id) result id for a target, which this
    prune never changes -- it only removes rows that were already superseded."""
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
        if result_table == "entry_commentary":
            conn.execute(
                "DELETE FROM entry_commentary WHERE entry_id = ? AND id != ?",
                (result_row["entry_id"], result_id),
            )
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
    a 'running' item never has a result row already written. Returns the count reset.

    Superseded by recover_or_cancel_running_jobs() below for jobs created through
    enqueue_snapshot_regen_job() (which requires a context snapshot before ever resuming a job);
    this function remains for any job that predates the snapshot column."""
    cur = conn.execute(
        "UPDATE regen_job_item SET status = 'pending', updated_at = ? WHERE status = 'running'",
        (_now(),),
    )
    return cur.rowcount


# ---------------------------------------------------------------------------
# Generation-safety migration
# ---------------------------------------------------------------------------

def migrate_generation_safety(conn: sqlite3.Connection) -> None:
    """Add the additive columns the maintenance-gate/lease/snapshot/fingerprint foundations need
    on regen_job and regen_job_item. Idempotent and safe on every startup, following the exact
    ALTER-TABLE pattern as migrate_persona_prompt_model/migrate_chat_session — a brand-new
    database also needs this run once (init_schema()'s CREATE TABLE IF NOT EXISTS alone does not
    add columns to an already-created table, and app.py always runs every migration after
    init_schema() regardless of whether the database is new or old).

    - regen_job.snapshot_entry_count stays NULL for every pre-existing job (there is no way to
      reconstruct what its archive membership was), which is exactly the signal
      recover_or_cancel_running_jobs() uses to treat it as an unrecoverable legacy job.
    - regen_job_item.request_format_version/request_fingerprint/baseline_result_id stay NULL for
      pre-existing items for the same reason: retry_failed_job_item() refuses to retry an item
      with no recorded baseline.
    """
    job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(regen_job)")}
    if "snapshot_entry_count" not in job_columns:
        conn.execute("ALTER TABLE regen_job ADD COLUMN snapshot_entry_count INTEGER")

    item_columns = {row["name"] for row in conn.execute("PRAGMA table_info(regen_job_item)")}
    if "request_format_version" not in item_columns:
        conn.execute("ALTER TABLE regen_job_item ADD COLUMN request_format_version INTEGER")
    if "request_fingerprint" not in item_columns:
        conn.execute("ALTER TABLE regen_job_item ADD COLUMN request_fingerprint TEXT")
    if "baseline_result_id" not in item_columns:
        conn.execute("ALTER TABLE regen_job_item ADD COLUMN baseline_result_id INTEGER")


def migrate_prune_entry_commentary_history(conn: sqlite3.Connection) -> None:
    """Collapse every entry's entry_commentary rows down to just the one it would already show as
    "current", deleting the rest. One-time cleanup for databases created before complete_job_item()
    started pruning older rows itself (see its docstring) -- entries only ever need their latest
    reflection. Idempotent: a database already collapsed to one row per entry_id is a no-op.

    Registered in _migrate_database_schema() for symmetry with the other migrations (so a fresh
    install, which starts with zero entry_commentary rows anyway, stays a true no-op there), but
    deliberately NOT invoked from initialize_database()'s already-bootstrapped fast path the way a
    schema-only migration would be: unlike an ALTER TABLE, this mutates row counts, and the
    "routine" deploy's restore drill (see unflincher-restore-drill.sh) asserts every table's row
    count is IDENTICAL before and after a plain restart against a restored backup, to catch
    accidental data loss. Running this automatically on every restart would trip that invariant
    (and rightly so -- from the drill's point of view it can't tell "intentional prune" from "bug").
    Applying this to an already-live database is therefore a deliberate, separate, manual step
    (see docs/deployment.md), not something a routine code deploy triggers by itself.

    "Current" here is the SAME rule get_current_commentary()/_current_result_id_for_target() use:
    the highest-id row with status='ok'. If an entry somehow has no 'ok' row at all (never produced
    by this app's own write path -- complete_job_item only ever inserts status='ok' -- but possible
    from historical data), the highest-id row of any status is kept instead, so a prune never
    discards an entry's only row. aggregate_report is never touched by this function."""
    entry_ids = [
        row["entry_id"] for row in conn.execute(
            "SELECT DISTINCT entry_id FROM entry_commentary ORDER BY entry_id"
        ).fetchall()
    ]
    for entry_id in entry_ids:
        keep_row = conn.execute(
            "SELECT id FROM entry_commentary WHERE entry_id = ? AND status = 'ok' "
            "ORDER BY id DESC LIMIT 1",
            (entry_id,),
        ).fetchone()
        if keep_row is None:
            keep_row = conn.execute(
                "SELECT id FROM entry_commentary WHERE entry_id = ? ORDER BY id DESC LIMIT 1",
                (entry_id,),
            ).fetchone()
        conn.execute(
            "DELETE FROM entry_commentary WHERE entry_id = ? AND id != ?",
            (entry_id, keep_row["id"]),
        )


# ---------------------------------------------------------------------------
# Current-result selection compatibility (created_at -> highest successful ID)
# ---------------------------------------------------------------------------

class CurrentResultSelectionAmbiguousError(RuntimeError):
    """Raised by verify_current_result_selection_compatibility() when v0.1's latest-created_at
    rule and v0.2's highest-successful-ID rule disagree on "current" for some entry_commentary or
    aggregate_report target. .conflicts lists every such target as a dict: "target" plus either
    "ids" (a tie at the max created_at -- v0.1 has no deterministic winner) or
    "v01_selected_id"/"highest_id" (an unambiguous mismatch, e.g. clock skew or a backdated row).
    Changes no data; the caller must resolve the conflict explicitly."""

    def __init__(self, conflicts: list[dict]):
        self.conflicts = conflicts
        super().__init__(
            f"current-result selection compatibility check failed for {len(conflicts)} "
            f"target(s): {conflicts}"
        )


def _check_target_result_selection_compatibility(target_label: str, rows: list[sqlite3.Row]) -> dict | None:
    """rows: every 'ok' row for one target (.id, .created_at). None if compatible; a target with
    no rows is trivially compatible."""
    if not rows:
        return None
    max_created_at = max(r["created_at"] for r in rows)
    tied_ids = sorted(r["id"] for r in rows if r["created_at"] == max_created_at)
    if len(tied_ids) > 1:
        return {"target": target_label, "reason": "tied_max_created_at", "ids": tied_ids}
    v01_selected_id = tied_ids[0]
    highest_id = max(r["id"] for r in rows)
    if v01_selected_id != highest_id:
        return {
            "target": target_label, "reason": "mismatch",
            "v01_selected_id": v01_selected_id, "highest_id": highest_id,
        }
    return None


def verify_current_result_selection_compatibility(conn: sqlite3.Connection) -> None:
    """Read-only, idempotent, independently callable (see migrate_bootstrap_state, the one-time
    caller during initialization, and any later offline migration tool). For every
    entry_commentary target and the aggregate_report target, verifies v0.1's "latest created_at"
    and v0.2's "highest successful ID" rules agree (see CurrentResultSelectionAmbiguousError).
    Raises listing every conflicting target, in deterministic entry_id order, so they can all be
    resolved at once. Never writes anything. A database missing entry_commentary/aggregate_report
    entirely (e.g. a minimal/malformed schema) has nothing to check for that table."""
    conflicts = []
    existing_tables = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }

    if "entry_commentary" in existing_tables:
        entry_ids = [
            row["entry_id"] for row in conn.execute(
                "SELECT DISTINCT entry_id FROM entry_commentary WHERE status = 'ok' ORDER BY entry_id"
            ).fetchall()
        ]
        for entry_id in entry_ids:
            rows = conn.execute(
                "SELECT id, created_at FROM entry_commentary WHERE entry_id = ? AND status = 'ok'",
                (entry_id,),
            ).fetchall()
            conflict = _check_target_result_selection_compatibility(f"entry_commentary:{entry_id}", rows)
            if conflict is not None:
                conflicts.append(conflict)

    if "aggregate_report" in existing_tables:
        report_rows = conn.execute(
            "SELECT id, created_at FROM aggregate_report WHERE status = 'ok'"
        ).fetchall()
        conflict = _check_target_result_selection_compatibility("aggregate_report", report_rows)
        if conflict is not None:
            conflicts.append(conflict)

    if conflicts:
        raise CurrentResultSelectionAmbiguousError(conflicts)


# ---------------------------------------------------------------------------
# Maintenance gate
# ---------------------------------------------------------------------------

class MaintenanceLockedError(RuntimeError):
    """New generation work is blocked because maintenance is locked. Stable and retryable (maps
    to a retryable maintenance response) — no write occurs on this path. The only two paths
    exempt from this check are recover_or_cancel_running_jobs() (finishing work admitted before
    maintenance began) and the local synthetic deployment probe (see probe.py); neither calls
    acquire_lease()/enqueue_snapshot_regen_job()/retry_failed_job_item()."""


class GenerationActivityError(RuntimeError):
    """Maintenance cannot finish while admitted generation work is still active."""

    def __init__(self, running_regen_job_ids: list[int], active_lease_count: int):
        self.running_regen_job_ids = running_regen_job_ids
        self.active_lease_count = active_lease_count
        details = []
        if running_regen_job_ids:
            details.append(
                "running regeneration job(s): "
                + ", ".join(str(job_id) for job_id in running_regen_job_ids)
            )
        if active_lease_count:
            details.append(f"active generation lease(s): {active_lease_count}")
        super().__init__("active generation remains: " + "; ".join(details))


class TargetBusyError(RuntimeError):
    """The target already has an active exclusive lease — another generation (direct, background,
    or a conversation turn) is currently using it. Stable, no-write, retryable."""

    def __init__(self, target_key: str):
        self.target_key = target_key
        super().__init__(f"target already has an active generation lease: {target_key}")


class ArchiveChangedError(RuntimeError):
    """The current ordered Journal Archive entry-ID list no longer matches the preflight snapshot
    the caller already validated every prepared request against — an entry was written after
    preflight. Stable 409 archive_changed; the enqueue transaction writes nothing. The caller must
    rebuild and revalidate its requests against the new archive before retrying."""


class RequestFormatChangedError(RuntimeError):
    """A reconstructed prepared request's assembly version or fingerprint no longer matches what
    was stored at enqueue time — the code that assembles requests changed since this item was
    admitted. Stable 409 request_format_changed; the item is not retried under the new format."""


class StaleOrSupersededRetryError(RuntimeError):
    """A failed regen_job_item is not eligible for retry: it is missing, not failed, belongs to a
    snapshot-less legacy job, has no recorded baseline, its owning job is not yet 'done' (another
    worker may still be actively driving it), its target's current result has advanced past its
    baseline (superseded by newer work), or a newer same-target item already exists. Stable,
    no-write, and NOT automatically retryable — the owner must start fresh generation."""


class ItemJobMismatchError(RuntimeError):
    """The item_id being retried does not belong to the job_id given in the request (e.g. a stale
    or hand-crafted URL). Stable, no-write -- maps to a 404, since the (job_id, item_id) resource
    the URL names does not exist, distinct from StaleOrSupersededRetryError's "exists but is not
    currently retryable" semantics."""

    def __init__(self, item_id: int, expected_job_id: int, actual_job_id: int):
        self.item_id = item_id
        self.expected_job_id = expected_job_id
        self.actual_job_id = actual_job_id
        super().__init__(
            f"item {item_id} belongs to job {actual_job_id}, not the requested job {expected_job_id}"
        )


def get_maintenance_locked(conn: sqlite3.Connection) -> bool:
    """Whether new generation work is currently blocked."""
    row = conn.execute("SELECT locked FROM maintenance_control WHERE id = 1").fetchone()
    if row is None:
        raise RuntimeError("maintenance_control singleton row (id=1) is missing")
    return bool(row["locked"])


def set_maintenance_locked(conn: sqlite3.Connection, locked: bool) -> None:
    """Set (or clear) the maintenance flag. The deploy procedure sets this BEFORE waiting for
    leases/jobs to drain, and clears it only after the target service is verified healthy — see
    the plan's Maintenance gate section. This function only flips the flag; draining and health
    verification are the caller's (deploy tooling's) responsibility."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = conn.execute(
            "UPDATE maintenance_control SET locked = ?, updated_at = ? WHERE id = 1",
            (1 if locked else 0, _now()),
        )
        if result.rowcount != 1:
            raise RuntimeError("maintenance_control singleton row (id=1) is missing")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def lock_maintenance_for_bootstrap(conn: sqlite3.Connection) -> None:
    """Create the v0.2 gate if needed and make the first upgrade write fail-locked."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS maintenance_control ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), "
            "locked INTEGER NOT NULL DEFAULT 0, "
            "updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        conn.execute("INSERT OR IGNORE INTO maintenance_control (id, locked) VALUES (1, 1)")
        conn.execute(
            "UPDATE maintenance_control SET locked = 1, updated_at = ? WHERE id = 1",
            (_now(),),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_generation_activity(conn: sqlite3.Connection) -> dict[str, object]:
    """Return the non-private drain state used by maintenance tooling."""
    tables = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    running_job_ids = []
    if "regen_job" in tables:
        running_job_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM regen_job WHERE status = 'running' ORDER BY id"
            ).fetchall()
        ]
    active_lease_count = 0
    if "generation_lease" in tables:
        active_lease_count = conn.execute(
            "SELECT COUNT(*) AS count FROM generation_lease"
        ).fetchone()["count"]
    return {
        "active_lease_count": active_lease_count,
        "running_regen_job_ids": running_job_ids,
    }


def require_no_running_regen_jobs(conn: sqlite3.Connection) -> None:
    """Abort offline bootstrap before writes if a prior service still owns a running job."""
    activity = get_generation_activity(conn)
    running_job_ids = activity["running_regen_job_ids"]
    if running_job_ids:
        raise GenerationActivityError(running_job_ids, 0)


def require_generation_idle(conn: sqlite3.Connection) -> None:
    """Require every admitted generation job and lease to have drained."""
    activity = get_generation_activity(conn)
    if activity["running_regen_job_ids"] or activity["active_lease_count"]:
        raise GenerationActivityError(
            activity["running_regen_job_ids"],
            activity["active_lease_count"],
        )


def unlock_maintenance_if_idle(conn: sqlite3.Connection) -> None:
    """Atomically verify the drain state and clear maintenance under one writer lock."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        activity = get_generation_activity(conn)
        if activity["running_regen_job_ids"] or activity["active_lease_count"]:
            raise GenerationActivityError(
                activity["running_regen_job_ids"],
                activity["active_lease_count"],
            )
        result = conn.execute(
            "UPDATE maintenance_control SET locked = 0, updated_at = ? WHERE id = 1",
            (_now(),),
        )
        if result.rowcount != 1:
            raise RuntimeError("maintenance_control singleton row (id=1) is missing")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def entry_target_key(entry_id: int) -> str:
    return f"entry:{entry_id}"


def report_target_key() -> str:
    return "report"


def entry_thread_key(entry_id: int) -> str:
    return f"entry-thread:{entry_id}"


def conversation_thread_key(session_id: int) -> str:
    return f"conversation:{session_id}"


def new_request_lease_key() -> str:
    """One centralized constructor for temporary request-scoped lease keys -- used by every path
    that needs a lease before a durable target exists yet (a brand-new Conversation, Prompt
    Workshop preview, optional title generation). Namespaced under "request:" and suffixed with a
    fresh UUID4, so independent concurrent preview/title/new-conversation calls can never collide
    with each other or with any other target key in this app (entry:<id>, report,
    entry-thread:<id>, conversation:<id> all use different prefixes with no UUID component)."""
    return f"request:{uuid.uuid4().hex}"


def acquire_lease(conn: sqlite3.Connection, target_key: str, lease_kind: str, owner_token: str) -> int:
    """Atomically (BEGIN IMMEDIATE): refuse if maintenance is locked, otherwise insert an
    exclusive lease row for target_key. Raises MaintenanceLockedError or TargetBusyError, writing
    nothing on either path. Used by every model-calling path, apply-all enqueue, single-entry job,
    and retry — the ONE seam maintenance and target exclusivity are enforced through."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if get_maintenance_locked(conn):
            raise MaintenanceLockedError(f"maintenance locked: cannot acquire lease for {target_key}")
        try:
            cur = conn.execute(
                "INSERT INTO generation_lease (target_key, lease_kind, owner_token, created_at) "
                "VALUES (?, ?, ?, ?)",
                (target_key, lease_kind, owner_token, _now()),
            )
        except sqlite3.IntegrityError as exc:
            raise TargetBusyError(target_key) from exc
        conn.execute("COMMIT")
        return cur.lastrowid
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _acquire_lease_bypassing_maintenance(
    conn: sqlite3.Connection, target_key: str, lease_kind: str, owner_token: str
) -> int:
    """Like acquire_lease() but does NOT consult the maintenance flag. Used ONLY by
    recover_or_cancel_running_jobs() to reacquire leases for an already-admitted, snapshot-backed
    job — one of exactly two maintenance bypasses (see MaintenanceLockedError). Never call this
    for new work. No transaction control of its own: the caller wraps it."""
    try:
        cur = conn.execute(
            "INSERT INTO generation_lease (target_key, lease_kind, owner_token, created_at) "
            "VALUES (?, ?, ?, ?)",
            (target_key, lease_kind, owner_token, _now()),
        )
    except sqlite3.IntegrityError as exc:
        raise TargetBusyError(target_key) from exc
    return cur.lastrowid


def release_lease(conn: sqlite3.Connection, lease_id: int) -> None:
    """Release one lease by id. Callers must always release in `finally`."""
    conn.execute("DELETE FROM generation_lease WHERE id = ?", (lease_id,))


def release_lease_by_target(conn: sqlite3.Connection, target_key: str) -> None:
    conn.execute("DELETE FROM generation_lease WHERE target_key = ?", (target_key,))


def get_lease_by_target(conn: sqlite3.Connection, target_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM generation_lease WHERE target_key = ?", (target_key,)
    ).fetchone()


def convert_lease_target(conn: sqlite3.Connection, lease_id: int, new_target_key: str) -> None:
    """Atomically repoint an existing lease to a new target_key, in its OWN transaction. Raises
    TargetBusyError (no write) if new_target_key already has a different active lease.

    NOT used for the new-general-Conversation handoff (creating the session/first message and
    converting the lease must be one atomic unit, not two separate transactions) -- see
    create_general_chat_session_and_convert_lease() below for that combined operation. This
    function remains for any simpler single-lease repoint that does not also need to write other
    rows in the same transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        try:
            cur = conn.execute(
                "UPDATE generation_lease SET target_key = ? WHERE id = ?",
                (new_target_key, lease_id),
            )
        except sqlite3.IntegrityError as exc:
            raise TargetBusyError(new_target_key) from exc
        if cur.rowcount == 0:
            raise ValueError(f"no lease with id {lease_id} to convert")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


class RequestLeaseExpiredError(RuntimeError):
    """The temporary request-scoped lease a new-Conversation admission depended on no longer
    exists by the time of the session/message handoff (e.g. evicted by a maintenance drain).
    Stable, no-write -- the caller must treat this like any other admission failure and never
    create the session/message."""


def create_general_chat_session_and_convert_lease(
    conn: sqlite3.Connection, *, request_lease_id: int, title: str, first_message: str,
) -> int:
    """The new-general-Conversation handoff, as ONE BEGIN IMMEDIATE transaction (never a separate
    convert_lease_target() call): verify the temporary request lease acquired before preflight
    still exists, create the session and its first user message, and convert that SAME lease to
    conversation:<session_id> -- all atomically.

    Deliberately does NOT re-check the maintenance flag here: if maintenance became locked after
    the request lease was acquired (and preflight/validation already completed against the
    pre-lock state), that existing lease is itself the proof this request was already admitted
    before the lock -- the deploy drain waits for it to finish rather than this handoff refusing
    it. Raises RequestLeaseExpiredError (no write) if the lease was somehow removed in the
    meantime (e.g. forcibly cleared) -- there is nothing left proving admission.

    Returns the new session_id."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        lease = conn.execute(
            "SELECT * FROM generation_lease WHERE id = ?", (request_lease_id,)
        ).fetchone()
        if lease is None:
            raise RequestLeaseExpiredError(
                f"request lease {request_lease_id} no longer exists; admission was revoked "
                "before the session could be created"
            )
        session_cur = conn.execute(
            "INSERT INTO chat_session (title, created_at, updated_at) VALUES (?, ?, ?)",
            (title, _now(), _now()),
        )
        session_id = session_cur.lastrowid
        conn.execute(
            "INSERT INTO chat_message (thread_kind, session_id, role, content) "
            "VALUES ('general', ?, 'user', ?)",
            (session_id, first_message),
        )
        try:
            conn.execute(
                "UPDATE generation_lease SET target_key = ? WHERE id = ?",
                (conversation_thread_key(session_id), request_lease_id),
            )
        except sqlite3.IntegrityError as exc:
            raise TargetBusyError(conversation_thread_key(session_id)) from exc
        conn.execute("COMMIT")
        return session_id
    except Exception:
        conn.execute("ROLLBACK")
        raise


def clear_stale_leases(conn: sqlite3.Connection) -> int:
    """Startup: delete every existing lease row. This app runs as a single process with one
    shared connection (see Global Constraints), so any lease still present at startup necessarily
    belongs to a process that is no longer running — nothing currently legitimately holds it.
    recover_or_cancel_running_jobs() re-acquires fresh leases for any snapshot-backed running job
    immediately afterward; a lease that cannot be re-acquired means that job is cancelled instead
    of silently left holding a stale lease. Returns the count removed."""
    cur = conn.execute("DELETE FROM generation_lease")
    return cur.rowcount


# ---------------------------------------------------------------------------
# Ordered archive snapshot
# ---------------------------------------------------------------------------

def get_ordered_entry_ids(conn: sqlite3.Connection) -> list[int]:
    """The canonical Journal Archive order: (entry_date ASC, id ASC). Entries are insert-only and
    never edited/deleted, so this order — captured once at preflight/enqueue time as an explicit
    per-row ordinal (see regen_job_entry_snapshot) — is what every background job, retry, and
    crash recovery replays, never live SQL `IN`/array order."""
    rows = conn.execute("SELECT id FROM diary_entry ORDER BY entry_date ASC, id ASC").fetchall()
    return [r["id"] for r in rows]


def get_job_entry_snapshot(conn: sqlite3.Connection, job_id: int) -> list[int]:
    """The stored ordered entry-ID snapshot for one job, in ordinal order (never `IN` order)."""
    rows = conn.execute(
        "SELECT entry_id FROM regen_job_entry_snapshot WHERE job_id = ? ORDER BY ordinal ASC",
        (job_id,),
    ).fetchall()
    return [r["entry_id"] for r in rows]


def get_entries_in_order(conn: sqlite3.Connection, entry_ids: list[int]) -> list[sqlite3.Row]:
    """Fetch full diary_entry rows for exactly the given IDs, returned in the SAME order as
    entry_ids -- never SQL `IN` order (SQLite does not guarantee it matches the placeholder list)
    and never a live `ORDER BY entry_date` re-query. This is the one place worker.py and the
    enqueue-preparation path turn a stored/preflight ordinal-ordered ID list back into entry
    content, so a same-date insert after enqueue can never reorder what a job or preflight already
    committed to.

    Silently SKIPS any id that no longer resolves to a row, rather than raising -- entries are
    insert-only and never deleted by this app, so a missing id means corruption, not a normal
    state. Callers that need to detect that (worker.py, recovery, retry admission) do so by
    comparing len(result) against len(entry_ids), not by relying on an exception here."""
    if not entry_ids:
        return []
    placeholders = ", ".join("?" for _ in entry_ids)
    rows = conn.execute(
        f"SELECT * FROM diary_entry WHERE id IN ({placeholders})", tuple(entry_ids)
    ).fetchall()
    by_id = {row["id"]: row for row in rows}
    return [by_id[eid] for eid in entry_ids if eid in by_id]


# ---------------------------------------------------------------------------
# Atomic snapshot+lease enqueue
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PreparedRegenTarget:
    """One already-preflighted generation target for enqueue_snapshot_regen_job(). Carries only
    the request-assembly identity (version + fingerprint) computed by the caller (see
    request_envelope.py) — this module has no LLM/prompt-assembly knowledge of its own.

    Validated at construction time (__post_init__) so a caller bug can never silently produce a
    target that "proceeds" through enqueue/retry/recovery with a missing or placeholder identity:
    an entry_commentary target without entry_id, an aggregate_report target WITH one, a
    non-positive request_format_version, or an empty request_fingerprint are all constructor-time
    errors, never a 0/""-defaulted value that looks superficially valid downstream."""

    target_type: str  # 'entry_commentary' | 'aggregate_report'
    entry_id: int | None
    request_format_version: int
    request_fingerprint: str

    def __post_init__(self) -> None:
        if self.target_type not in ("entry_commentary", "aggregate_report"):
            raise ValueError(f"invalid target_type: {self.target_type!r}")
        if self.target_type == "entry_commentary" and not self.entry_id:
            raise ValueError("entry_commentary target requires a truthy entry_id")
        if self.target_type == "aggregate_report" and self.entry_id is not None:
            raise ValueError("aggregate_report target must not carry an entry_id")
        if not isinstance(self.request_format_version, int) or self.request_format_version < 1:
            raise ValueError(f"invalid request_format_version: {self.request_format_version!r}")
        if not self.request_fingerprint:
            raise ValueError("request_fingerprint must be a non-empty string")

    @property
    def target_key(self) -> str:
        if self.target_type == "entry_commentary":
            return entry_target_key(self.entry_id)
        return report_target_key()


def _current_result_id_for_target(conn: sqlite3.Connection, target: PreparedRegenTarget) -> int | None:
    """The target's current successful result ID, by highest ID (not wall-clock text — see the
    plan's Persistence and migration section) — used as the admission-time baseline every new
    regen_job_item stores for retry's supersession check."""
    if target.target_type == "entry_commentary":
        row = conn.execute(
            "SELECT id FROM entry_commentary WHERE entry_id = ? AND status = 'ok' "
            "ORDER BY id DESC LIMIT 1",
            (target.entry_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM aggregate_report WHERE status = 'ok' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return row["id"] if row is not None else None


def enqueue_snapshot_regen_job(
    conn: sqlite3.Connection,
    *,
    prompt_version_id: int | None = None,
    activate_prompt: tuple[str, str] | None = None,
    activate_preset_key: str | None = None,
    preflight_entry_ids: list[int],
    targets: list[PreparedRegenTarget],
    owner_token: str,
) -> tuple[int, int | None]:
    """Atomically, under ONE BEGIN IMMEDIATE transaction:

    1. refuse if maintenance is locked (no write);
    2. compare the CURRENT canonical ordered archive entry-ID list against preflight_entry_ids —
       the list the caller already validated every prepared request against — and refuse (no
       write) with ArchiveChangedError if an entry was written after preflight;
    3. acquire an exclusive generation_lease for every target in `targets` (TargetBusyError, no
       write, if any target already has an active lease — this is what makes a direct single-
       target generation and an apply-all job mutually exclusive on the SAME target, not merely
       "only one job system-wide");
    4. if `activate_prompt` is given (body_text, model), atomically create and activate a new
       persona_prompt version in this SAME transaction (see
       _insert_activated_persona_prompt_version) and use its id as the job's prompt_version_id.
       `activate_preset_key` is an optional caller-claimed hint (unused today, never trusted --
       preset_key always comes from perspectives.classify_prompt(body_text)). Otherwise
       `prompt_version_id` (an existing version) is used unchanged. Exactly one of
       prompt_version_id/activate_prompt must be given.
    5. insert one regen_job row with its snapshot_entry_count;
    6. insert one regen_job_entry_snapshot row per entry, in the SAME order as
       preflight_entry_ids, each with an explicit ordinal;
    7. insert one regen_job_item per target, each carrying its own request_format_version,
       request_fingerprint, and admission-time baseline_result_id.

    Returns (job_id, activated_prompt_id) -- the second element is None unless activate_prompt was
    given. The existing ux_regen_job_one_running partial unique index still applies (this app's
    worker/recovery model handles one running job at a time) — a second concurrently-running job
    still raises a raw sqlite3.IntegrityError, exactly like start_regen_job. Any failure (busy
    target, maintenance, archive changed, single-flight job index) rolls back the uncommitted
    prompt/preset-key insert too -- apply-all never persists either when it rolls back."""
    del activate_preset_key  # server-side classification only -- see docstring
    if (prompt_version_id is None) == (activate_prompt is None):
        raise ValueError("exactly one of prompt_version_id or activate_prompt must be given")

    conn.execute("BEGIN IMMEDIATE")
    try:
        if get_maintenance_locked(conn):
            raise MaintenanceLockedError("maintenance locked: cannot enqueue a new regeneration job")

        current_entry_ids = conn.execute(
            "SELECT id FROM diary_entry ORDER BY entry_date ASC, id ASC"
        ).fetchall()
        current_entry_ids = [r["id"] for r in current_entry_ids]
        if current_entry_ids != list(preflight_entry_ids):
            raise ArchiveChangedError(
                "journal archive changed between preflight and enqueue; rebuild and revalidate"
            )

        for target in targets:
            try:
                conn.execute(
                    "INSERT INTO generation_lease (target_key, lease_kind, owner_token, created_at) "
                    "VALUES (?, 'background', ?, ?)",
                    (target.target_key, owner_token, _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise TargetBusyError(target.target_key) from exc

        activated_prompt_id = None
        if activate_prompt is not None:
            body_text, model = activate_prompt
            activated_prompt_id = _insert_activated_persona_prompt_version(conn, body_text, model)
            prompt_version_id = activated_prompt_id

        job_cur = conn.execute(
            "INSERT INTO regen_job (prompt_version_id, status, snapshot_entry_count, created_at, started_at) "
            "VALUES (?, 'running', ?, ?, ?)",
            (prompt_version_id, len(preflight_entry_ids), _now(), _now()),
        )
        job_id = job_cur.lastrowid

        for ordinal, entry_id in enumerate(preflight_entry_ids):
            conn.execute(
                "INSERT INTO regen_job_entry_snapshot (job_id, ordinal, entry_id) VALUES (?, ?, ?)",
                (job_id, ordinal, entry_id),
            )

        for target in targets:
            baseline_result_id = _current_result_id_for_target(conn, target)
            conn.execute(
                "INSERT INTO regen_job_item (job_id, target_type, entry_id, status, "
                "request_format_version, request_fingerprint, baseline_result_id, updated_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)",
                (
                    job_id, target.target_type, target.entry_id,
                    target.request_format_version, target.request_fingerprint,
                    baseline_result_id, _now(),
                ),
            )

        conn.execute("COMMIT")
        return job_id, activated_prompt_id
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# Atomic failed-item retry
# ---------------------------------------------------------------------------

def retry_failed_job_item(
    conn: sqlite3.Connection, *, item_id: int, owner_token: str, expected_job_id: int | None = None,
) -> int:
    """Atomically retry one failed regen_job_item under ONE BEGIN IMMEDIATE transaction. Returns
    the AUTHORITATIVE owning job_id so a caller (e.g. a route parsed from a URL) never has to
    trust its own job_id independently of the item's real ownership.

    Checks, in order (any failure writes nothing and raises without side effects):
    - the item exists (else StaleOrSupersededRetryError);
    - if expected_job_id is given, the item actually belongs to it (else ItemJobMismatchError --
      a stale/mismatched URL, mapped to a 404, distinct from "exists but not retryable");
    - the item is currently 'failed' (else StaleOrSupersededRetryError);
    - maintenance is not locked (MaintenanceLockedError);
    - its job has a context snapshot — a legacy/snapshot-less job is never retryable
      (StaleOrSupersededRetryError);
    - its job is 'done' — a job another worker is still actively driving (status='running') must
      never be handed a second worker for the same job_id (StaleOrSupersededRetryError);
    - the item has a recorded request_format_version/request_fingerprint (else
      StaleOrSupersededRetryError — a pre-migration item has no trustworthy identity to rerun);
    - its persisted baseline_result_id still equals the target's CURRENT successful result
      (else StaleOrSupersededRetryError — newer work has already superseded it);
    - no newer job item exists for the same target (else StaleOrSupersededRetryError);
    - the target's exclusive lease is free (else TargetBusyError).

    On success: acquires the target's lease, flips the item back to 'pending' (clearing its
    error), and reopens its job to 'running'. Reopening can itself raise a raw
    sqlite3.IntegrityError via the existing ux_regen_job_one_running index if a DIFFERENT job is
    currently running — the single-flight rule this app already enforces.

    This is the FINAL, authoritative check. Callers that need to refuse BEFORE ever reaching this
    transaction (e.g. because the reconstructed request's fingerprint no longer matches, or the
    model's current limit no longer admits it) must perform that validation first and only call
    this function once it succeeds — see regen_enqueue.retry_job_item_with_admission()."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        item = conn.execute("SELECT * FROM regen_job_item WHERE id = ?", (item_id,)).fetchone()
        if item is None:
            raise StaleOrSupersededRetryError(f"item {item_id} not found")
        if expected_job_id is not None and item["job_id"] != expected_job_id:
            raise ItemJobMismatchError(item_id, expected_job_id, item["job_id"])
        if item["status"] != "failed":
            raise StaleOrSupersededRetryError(f"item {item_id} not found or not in a failed state")

        if get_maintenance_locked(conn):
            raise MaintenanceLockedError("maintenance locked: cannot retry a failed item")

        job = conn.execute("SELECT * FROM regen_job WHERE id = ?", (item["job_id"],)).fetchone()
        if job is None or job["snapshot_entry_count"] is None:
            raise StaleOrSupersededRetryError(
                f"job {item['job_id']} has no context snapshot (legacy job); not retryable"
            )
        if job["status"] != "done":
            raise StaleOrSupersededRetryError(
                f"job {item['job_id']} is not done (status={job['status']!r}); another worker may "
                "still be actively driving it -- refusing to start a second one"
            )

        if not item["request_format_version"] or not item["request_fingerprint"]:
            raise StaleOrSupersededRetryError(
                f"item {item_id} has no recorded request format/fingerprint; not retryable"
            )

        target = PreparedRegenTarget(
            target_type=item["target_type"],
            entry_id=item["entry_id"],
            request_format_version=item["request_format_version"],
            request_fingerprint=item["request_fingerprint"],
        )
        current_result_id = _current_result_id_for_target(conn, target)
        if current_result_id != item["baseline_result_id"]:
            raise StaleOrSupersededRetryError(
                f"target {target.target_key}'s current result has advanced past this item's "
                "baseline; superseded by newer work"
            )

        if item["entry_id"] is None:
            newer_item = conn.execute(
                "SELECT id FROM regen_job_item WHERE target_type = ? AND entry_id IS NULL AND id > ?",
                (item["target_type"], item["id"]),
            ).fetchone()
        else:
            newer_item = conn.execute(
                "SELECT id FROM regen_job_item WHERE target_type = ? AND entry_id = ? AND id > ?",
                (item["target_type"], item["entry_id"], item["id"]),
            ).fetchone()
        if newer_item is not None:
            raise StaleOrSupersededRetryError(
                f"a newer job item exists for target {target.target_key}"
            )

        try:
            conn.execute(
                "INSERT INTO generation_lease (target_key, lease_kind, owner_token, created_at) "
                "VALUES (?, 'background', ?, ?)",
                (target.target_key, owner_token, _now()),
            )
        except sqlite3.IntegrityError as exc:
            raise TargetBusyError(target.target_key) from exc

        conn.execute(
            "UPDATE regen_job_item SET status = 'pending', error = NULL, updated_at = ? WHERE id = ?",
            (_now(), item_id),
        )
        conn.execute(
            "UPDATE regen_job SET status = 'running', finished_at = NULL WHERE id = ? AND status = 'done'",
            (item["job_id"],),
        )
        conn.execute("COMMIT")
        return item["job_id"]
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# Snapshot-aware startup recovery
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RecoveryResult:
    recovered_job_ids: list
    cancelled_job_ids: list
    cancelled_item_count: int


def _cancel_job_and_delete_unfinished_items(conn: sqlite3.Connection, job_id: int) -> int:
    cur = conn.execute(
        "DELETE FROM regen_job_item WHERE job_id = ? AND status IN ('pending', 'running')",
        (job_id,),
    )
    deleted = cur.rowcount
    conn.execute(
        "UPDATE regen_job SET status = 'cancelled', finished_at = ? WHERE id = ?",
        (_now(), job_id),
    )
    return deleted


def _job_snapshot_is_valid(conn: sqlite3.Connection, job: sqlite3.Row, snapshot_ids: list) -> bool:
    """Defense-in-depth validation a job must pass before recovery may reacquire its leases and
    resume it — never trust snapshot_entry_count or the stored entry IDs blindly:

    - the job's own prompt version must still exist (its immutable persona_prompt row);
    - the stored regen_job_entry_snapshot row COUNT must exactly equal snapshot_entry_count (a
      partially written or corrupted snapshot is never silently treated as complete);
    - every snapshotted entry_id must still resolve to an actual diary_entry row (entries are
      insert-only and never deleted by this app, so a missing one indicates corruption, not a
      normal state).

    snapshot_ids is the caller's own already-fetched get_job_entry_snapshot(conn, job["id"])
    result -- passed in rather than re-queried here so the caller can also use the exact same
    list for its own per-item snapshot-membership check (see recover_or_cancel_running_jobs).

    A job failing any of these checks must be cancelled with its unfinished items deleted, never
    marked recovered — resuming it could silently generate from truncated/wrong context."""
    prompt_exists = conn.execute(
        "SELECT 1 FROM persona_prompt WHERE id = ?", (job["prompt_version_id"],)
    ).fetchone()
    if prompt_exists is None:
        return False

    if len(snapshot_ids) != job["snapshot_entry_count"]:
        return False
    if not snapshot_ids:
        return True

    placeholders = ", ".join("?" for _ in snapshot_ids)
    existing_count = conn.execute(
        f"SELECT COUNT(*) AS n FROM diary_entry WHERE id IN ({placeholders})", tuple(snapshot_ids)
    ).fetchone()["n"]
    return existing_count == len(snapshot_ids)


def recover_or_cancel_running_jobs(conn: sqlite3.Connection, owner_token: str) -> RecoveryResult:
    """Startup recovery, run once before the app accepts requests. Atomic under one BEGIN
    IMMEDIATE.

    1. clear_stale_leases(): every lease row present at startup belongs to the previous (now dead)
       process — this is a single-process app (see Global Constraints).
    2. For every regen_job left 'running' from a hard crash:
       - NO context snapshot (snapshot_entry_count IS NULL — a legacy job, or one from code that
         predates this feature): cancel it and delete its unfinished (pending/running) items so no
         older retry route can requeue them. NEVER resumed against the live archive.
       - HAS a snapshot: reacquire the exact target lease for each of its still-unfinished items,
         bypassing the maintenance flag (this is one of exactly two allowed bypasses — finishing
         work admitted before maintenance began). If any target lease cannot be acquired (a
         defense-in-depth check; should not normally happen immediately after clearing stale
         leases), the job is cancelled instead of left permanently running. Otherwise its
         'running' items are reset to 'pending' so the relaunched worker re-claims them, and the
         job's own leases are recorded as held for the remainder of this process's lifetime.

    Returns which jobs were recovered vs. cancelled, and how many items were discarded."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        clear_stale_leases(conn)
        running_jobs = conn.execute("SELECT * FROM regen_job WHERE status = 'running'").fetchall()
        recovered: list = []
        cancelled: list = []
        cancelled_item_count = 0

        for job in running_jobs:
            job_id = job["id"]
            if job["snapshot_entry_count"] is None:
                cancelled_item_count += _cancel_job_and_delete_unfinished_items(conn, job_id)
                cancelled.append(job_id)
                continue

            items = conn.execute(
                "SELECT * FROM regen_job_item WHERE job_id = ? AND status IN ('pending', 'running')",
                (job_id,),
            ).fetchall()
            snapshot_ids = get_job_entry_snapshot(conn, job_id)
            snapshot_id_set = set(snapshot_ids)

            # Any item missing a valid request identity (no/blank format version or fingerprint,
            # an entry_commentary item without an entry_id, an entry_commentary item whose
            # entry_id is NOT a member of this job's own stored snapshot -- a valid, currently
            # existing entry outside the snapshot must never be silently accepted just because it
            # resolves -- or an aggregate_report item WITH an entry_id -- e.g. corrupted state, or
            # a snapshot column populated by future code without also populating these) makes the
            # WHOLE job ineligible for recovery: there is nothing trustworthy to reconstruct/
            # verify before ever calling the model again. Never build a placeholder
            # PreparedRegenTarget with a defaulted 0/"" identity to "make it fit" — cancel instead.
            has_valid_identity = all(
                i["request_format_version"] and i["request_fingerprint"] and (
                    (i["entry_id"] is not None and i["entry_id"] in snapshot_id_set)
                    if i["target_type"] == "entry_commentary"
                    else i["entry_id"] is None
                )
                for i in items
            )
            if not has_valid_identity or not _job_snapshot_is_valid(conn, job, snapshot_ids):
                cancelled_item_count += _cancel_job_and_delete_unfinished_items(conn, job_id)
                cancelled.append(job_id)
                continue

            targets = [
                PreparedRegenTarget(
                    target_type=i["target_type"],
                    entry_id=i["entry_id"],
                    request_format_version=i["request_format_version"],
                    request_fingerprint=i["request_fingerprint"],
                )
                for i in items
            ]

            acquired_lease_ids: list = []
            busy = False
            for target in targets:
                try:
                    lease_id = _acquire_lease_bypassing_maintenance(
                        conn, target.target_key, "background", owner_token
                    )
                    acquired_lease_ids.append(lease_id)
                except TargetBusyError:
                    busy = True
                    break

            if busy:
                for lease_id in acquired_lease_ids:
                    release_lease(conn, lease_id)
                cancelled_item_count += _cancel_job_and_delete_unfinished_items(conn, job_id)
                cancelled.append(job_id)
                continue

            conn.execute(
                "UPDATE regen_job_item SET status = 'pending', updated_at = ? "
                "WHERE job_id = ? AND status = 'running'",
                (_now(), job_id),
            )
            recovered.append(job_id)

        conn.execute("COMMIT")
        return RecoveryResult(
            recovered_job_ids=recovered, cancelled_job_ids=cancelled,
            cancelled_item_count=cancelled_item_count,
        )
    except Exception:
        conn.execute("ROLLBACK")
        raise
