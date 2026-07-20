"""Tests for workstream 4 (backward-compatible Perspective persistence): crash-safe fresh-vs-
upgrade detection, Analyst seeding, exact-text preset classification, historical join aliases,
and the current-result-selection (created_at -> highest successful ID) compatibility verifier.
See the plan's Persistence and migration section and db.py's own docstrings for the invariants
under test."""
import pytest

from unflincher.db import (
    CurrentResultSelectionAmbiguousError,
    OfflineBootstrapRequiredError,
    PreparedRegenTarget,
    TargetBusyError,
    get_active_prompt,
    get_connection,
    get_current_commentary,
    get_current_report,
    get_report_by_id,
    init_schema,
    initialize_database,
    initialize_upgrade_database,
    list_report_versions,
    migrate_bootstrap_state,
    migrate_chat_session,
    migrate_generation_safety,
    migrate_persona_prompt_model,
    migrate_persona_prompt_preset_key,
    migrate_prune_entry_commentary_history,
    seed_analyst_prompt_if_fresh_install,
    set_active_prompt,
    set_maintenance_locked,
    verify_current_result_selection_compatibility,
)
from unflincher.perspectives import DEFAULT_PERSPECTIVE_KEY, get_preset


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "test.db")
    c = get_connection(db_path)
    migrate_bootstrap_state(c)
    init_schema(c)
    migrate_persona_prompt_model(c)
    migrate_persona_prompt_preset_key(c)
    migrate_chat_session(c)
    migrate_generation_safety(c)
    yield c
    c.close()


def _seed_entry(conn, title="e", entry_date="2026-01-01"):
    return conn.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES (?, '<p>x</p>', '<p>x</p>', 'x', ?, 'import')",
        (title, entry_date),
    ).lastrowid


def _insert_commentary(conn, entry_id, prompt_id, *, created_at, status="ok", model="test-model"):
    cur = conn.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status, created_at) "
        "VALUES (?, ?, ?, 'x', ?, ?)",
        (entry_id, prompt_id, model, status, created_at),
    )
    return cur.lastrowid


def _insert_report(conn, prompt_id, *, created_at, status="ok", model="test-model"):
    cur = conn.execute(
        "INSERT INTO aggregate_report (prompt_version_id, model, body_text, covered_entry_count, status, created_at) "
        "VALUES (?, ?, 'x', 1, ?, ?)",
        (prompt_id, model, status, created_at),
    )
    return cur.lastrowid


# ---------------------------------------------------------------------------
# migrate_prune_entry_commentary_history
# ---------------------------------------------------------------------------

def test_migrate_prune_entry_commentary_history_keeps_only_the_latest_ok_row(conn):
    entry_id = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "p", "test-model")
    _insert_commentary(conn, entry_id, prompt_id, created_at="2026-01-01T00:00:00+00:00")
    _insert_commentary(conn, entry_id, prompt_id, created_at="2026-01-02T00:00:00+00:00")
    newest_id = _insert_commentary(conn, entry_id, prompt_id, created_at="2026-01-03T00:00:00+00:00")

    migrate_prune_entry_commentary_history(conn)

    rows = conn.execute("SELECT id FROM entry_commentary WHERE entry_id = ?", (entry_id,)).fetchall()
    assert [r["id"] for r in rows] == [newest_id]


def test_migrate_prune_entry_commentary_history_prefers_the_latest_ok_row_over_a_later_failed_one(conn):
    # The highest id is a failed attempt with no successful text; the migration keeps the LATEST
    # OK row (matching get_current_commentary's own selection rule), never a failed row over a
    # usable one, so an entry never loses its displayed commentary to a later failed regen.
    entry_id = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "p", "test-model")
    ok_id = _insert_commentary(conn, entry_id, prompt_id, created_at="2026-01-01T00:00:00+00:00")
    _insert_commentary(conn, entry_id, prompt_id, created_at="2026-01-02T00:00:00+00:00", status="failed")

    migrate_prune_entry_commentary_history(conn)

    rows = conn.execute("SELECT id FROM entry_commentary WHERE entry_id = ?", (entry_id,)).fetchall()
    assert [r["id"] for r in rows] == [ok_id]


def test_migrate_prune_entry_commentary_history_keeps_the_only_row_when_none_succeeded(conn):
    entry_id = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "p", "test-model")
    only_id = _insert_commentary(conn, entry_id, prompt_id, created_at="2026-01-01T00:00:00+00:00", status="failed")

    migrate_prune_entry_commentary_history(conn)

    rows = conn.execute("SELECT id FROM entry_commentary WHERE entry_id = ?", (entry_id,)).fetchall()
    assert [r["id"] for r in rows] == [only_id]


def test_migrate_prune_entry_commentary_history_is_idempotent(conn):
    entry_id = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "p", "test-model")
    _insert_commentary(conn, entry_id, prompt_id, created_at="2026-01-01T00:00:00+00:00")
    newest_id = _insert_commentary(conn, entry_id, prompt_id, created_at="2026-01-02T00:00:00+00:00")

    migrate_prune_entry_commentary_history(conn)
    migrate_prune_entry_commentary_history(conn)  # second run must be a no-op

    rows = conn.execute("SELECT id FROM entry_commentary WHERE entry_id = ?", (entry_id,)).fetchall()
    assert [r["id"] for r in rows] == [newest_id]


def test_migrate_prune_entry_commentary_history_never_touches_aggregate_report(conn):
    entry_id = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "p", "test-model")
    _insert_commentary(conn, entry_id, prompt_id, created_at="2026-01-01T00:00:00+00:00")
    _insert_commentary(conn, entry_id, prompt_id, created_at="2026-01-02T00:00:00+00:00")
    _insert_report(conn, prompt_id, created_at="2026-01-01T00:00:00+00:00")
    _insert_report(conn, prompt_id, created_at="2026-01-02T00:00:00+00:00")

    migrate_prune_entry_commentary_history(conn)

    assert conn.execute("SELECT COUNT(*) AS n FROM aggregate_report").fetchone()["n"] == 2


# ---------------------------------------------------------------------------
# Fresh-vs-upgrade classification (migrate_bootstrap_state)
# ---------------------------------------------------------------------------

def test_migrate_bootstrap_state_classifies_a_brand_new_database_as_fresh(tmp_path):
    c = get_connection(str(tmp_path / "fresh.db"))
    migrate_bootstrap_state(c)
    row = c.execute("SELECT * FROM db_bootstrap_state WHERE id = 1").fetchone()
    assert row["is_fresh_install"] == 1
    assert row["analyst_seeded"] == 0  # not yet seeded -- seed_analyst_prompt_if_fresh_install does that
    c.close()


def test_migrate_bootstrap_state_classifies_pre_existing_persona_prompt_table_as_upgrade(tmp_path):
    # An existing (pre-workstream-4) database: persona_prompt table already exists, created by an
    # EARLIER process/release, even though it currently has ZERO rows -- exactly the edge case
    # item 3 calls out explicitly. Row count must never be used as the freshness signal.
    c = get_connection(str(tmp_path / "legacy-empty.db"))
    init_schema(c)  # persona_prompt now exists, zero rows, as if from an earlier release
    assert c.execute("SELECT COUNT(*) AS n FROM persona_prompt").fetchone()["n"] == 0

    migrate_bootstrap_state(c)

    row = c.execute("SELECT * FROM db_bootstrap_state WHERE id = 1").fetchone()
    assert row["is_fresh_install"] == 0
    assert row["analyst_seeded"] == 1  # forced immediately -- a permanent no-op for this DB
    c.close()


def test_migrate_bootstrap_state_classifies_pre_existing_populated_table_as_upgrade(tmp_path):
    c = get_connection(str(tmp_path / "legacy-populated.db"))
    init_schema(c)
    migrate_persona_prompt_model(c)
    set_active_prompt(c, "an existing custom persona", "gpt-5.4")

    migrate_bootstrap_state(c)

    row = c.execute("SELECT * FROM db_bootstrap_state WHERE id = 1").fetchone()
    assert row["is_fresh_install"] == 0
    assert row["analyst_seeded"] == 1
    c.close()


def test_application_initialization_requires_offline_bootstrap_for_partial_schema(tmp_path):
    """Regression test: a pre-v0.2 database that crashed partway through init_schema() -- some
    tables (e.g. diary_entry) exist, but persona_prompt itself does not yet -- is a partial
    schema, not a truly new install, and must never be classified as fresh or seeded. Freshness
    requires NO pre-existing user tables at all."""
    db_path = str(tmp_path / "partial-schema-crash.db")
    c = get_connection(db_path)
    c.execute(
        "CREATE TABLE diary_entry ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, "
        "content_html_raw TEXT NOT NULL, content_html TEXT NOT NULL, content_text TEXT NOT NULL, "
        "entry_date TEXT NOT NULL, source TEXT NOT NULL CHECK (source IN ('import', 'manual')), "
        "douban_url TEXT, source_modified_at TEXT, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    assert "persona_prompt" not in {
        row["name"] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }

    with pytest.raises(OfflineBootstrapRequiredError):
        initialize_database(c)

    tables = {
        row["name"] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "db_bootstrap_state" not in tables
    assert "persona_prompt" not in tables
    c.close()


def test_migrate_bootstrap_state_is_idempotent_and_preserves_classification(tmp_path):
    c = get_connection(str(tmp_path / "idempotent.db"))
    migrate_bootstrap_state(c)
    first = dict(c.execute("SELECT * FROM db_bootstrap_state WHERE id = 1").fetchone())

    migrate_bootstrap_state(c)  # second call must not re-derive or reset anything
    second = dict(c.execute("SELECT * FROM db_bootstrap_state WHERE id = 1").fetchone())

    assert first == second
    assert c.execute("SELECT COUNT(*) AS n FROM db_bootstrap_state").fetchone()["n"] == 1


def test_migrate_bootstrap_state_survives_a_crash_between_schema_creation_and_seeding(tmp_path):
    """Simulates the exact crash window item 3 calls out: an earlier process ran
    migrate_bootstrap_state + init_schema (committing db_bootstrap_state with
    is_fresh_install=1, analyst_seeded=0) but crashed before seed_analyst_prompt_if_fresh_install
    ever ran. The NEXT startup must still classify this as fresh-not-yet-seeded (never as an
    upgrade), so the Analyst seed is not silently skipped."""
    db_path = str(tmp_path / "crash-window.db")
    first_process = get_connection(db_path)
    migrate_bootstrap_state(first_process)
    # Crash here before schema creation or seed_analyst_prompt_if_fresh_install runs.
    first_process.close()

    second_process = get_connection(db_path)
    initialize_database(second_process)

    active = get_active_prompt(second_process)
    assert active is not None
    assert active["preset_key"] == "analyst"
    assert active["version_no"] == 1
    second_process.close()


def test_application_initialization_rejects_missing_maintenance_row_without_recreating_it(
    tmp_path,
):
    c = get_connection(str(tmp_path / "missing-maintenance-row.db"))
    initialize_database(c)
    c.execute("DELETE FROM maintenance_control WHERE id = 1")

    with pytest.raises(
        RuntimeError,
        match=r"maintenance_control singleton row \(id=1\) is missing",
    ):
        initialize_database(c)

    assert c.execute("SELECT COUNT(*) AS n FROM maintenance_control").fetchone()["n"] == 0
    c.close()


def test_initialize_database_never_prunes_entry_commentary_on_an_already_bootstrapped_restart(
    tmp_path,
):
    """Regression guard for the routine deploy's restore drill (see
    unflincher-restore-drill.sh), which asserts every table's row count is IDENTICAL before and
    after a plain restart against a restored backup, to catch accidental data loss. An ordinary
    app restart against an already-bootstrapped database takes initialize_database's early-return
    branch, which must NOT call migrate_prune_entry_commentary_history (or anything else that
    changes row counts) -- that migration is a deliberate, separate, manual step (see
    docs/deployment.md and migrate_prune_entry_commentary_history's own docstring), never
    something a routine restart triggers by itself."""
    db_path = str(tmp_path / "restart-no-prune.db")
    c = get_connection(db_path)
    initialize_database(c)  # first boot: fresh install, seeds bootstrap_state + Analyst prompt

    entry_id = _seed_entry(c)
    prompt_id = set_active_prompt(c, "p", "test-model")
    _insert_commentary(c, entry_id, prompt_id, created_at="2026-01-01T00:00:00+00:00")
    _insert_commentary(c, entry_id, prompt_id, created_at="2026-01-02T00:00:00+00:00")

    initialize_database(c)  # simulated restart: already bootstrapped, early-return branch

    assert (
        c.execute(
            "SELECT COUNT(*) AS n FROM entry_commentary WHERE entry_id = ?", (entry_id,)
        ).fetchone()["n"]
        == 2
    )
    c.close()


# ---------------------------------------------------------------------------
# One-time current-result-selection verification (item 2)
# ---------------------------------------------------------------------------

def test_migrate_bootstrap_state_marks_a_fresh_database_as_verified_with_nothing_to_check(tmp_path):
    c = get_connection(str(tmp_path / "fresh-verified.db"))
    migrate_bootstrap_state(c)
    row = c.execute("SELECT * FROM db_bootstrap_state WHERE id = 1").fetchone()
    assert row["current_result_selection_verified"] == 1
    c.close()


def test_migrate_bootstrap_state_verifies_and_marks_a_compatible_upgrade_database(tmp_path):
    c = get_connection(str(tmp_path / "upgrade-verified.db"))
    init_schema(c)
    migrate_persona_prompt_model(c)
    prompt_id = set_active_prompt(c, "p", "gpt-5.4")
    entry_id = _seed_entry(c)
    _insert_commentary(c, entry_id, prompt_id, created_at="2026-01-01T00:00:00+00:00")

    migrate_bootstrap_state(c)

    row = c.execute("SELECT * FROM db_bootstrap_state WHERE id = 1").fetchone()
    assert row["is_fresh_install"] == 0
    assert row["current_result_selection_verified"] == 1
    c.close()


def test_migrate_bootstrap_state_rejects_an_ambiguous_upgrade_database_and_writes_nothing(tmp_path):
    """A tie/mismatch on a pre-existing (unverified) database must abort BEFORE any mutation --
    no db_bootstrap_state row, no schema change, no touched prompt/result rows."""
    db_path = str(tmp_path / "ambiguous-upgrade.db")
    c = get_connection(db_path)
    init_schema(c)
    migrate_persona_prompt_model(c)
    prompt_id = set_active_prompt(c, "p", "gpt-5.4")
    entry_id = _seed_entry(c)
    same_ts = "2026-01-01T00:00:00+00:00"
    _insert_commentary(c, entry_id, prompt_id, created_at=same_ts)
    _insert_commentary(c, entry_id, prompt_id, created_at=same_ts)  # tie

    with pytest.raises(CurrentResultSelectionAmbiguousError):
        migrate_bootstrap_state(c)

    tables = {row["name"] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "db_bootstrap_state" not in tables
    assert c.execute("SELECT COUNT(*) AS n FROM entry_commentary").fetchone()["n"] == 2
    assert c.execute("SELECT COUNT(*) AS n FROM persona_prompt").fetchone()["n"] == 1
    c.close()


def test_migrate_bootstrap_state_treats_a_missing_singleton_row_as_an_explicit_error(tmp_path):
    """The durable marker is read explicitly, never inferred from mere table existence -- a
    db_bootstrap_state table with no id=1 row is a data-integrity violation, not "already
    verified"."""
    c = get_connection(str(tmp_path / "missing-row.db"))
    c.execute(
        "CREATE TABLE db_bootstrap_state (id INTEGER PRIMARY KEY CHECK (id = 1), "
        "is_fresh_install INTEGER NOT NULL, analyst_seeded INTEGER NOT NULL DEFAULT 0, "
        "current_result_selection_verified INTEGER NOT NULL DEFAULT 0, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )

    with pytest.raises(RuntimeError, match="singleton row"):
        migrate_bootstrap_state(c)
    c.close()


def test_migrate_bootstrap_state_reverifies_and_marks_an_existing_unverified_row(tmp_path):
    """If db_bootstrap_state already exists with current_result_selection_verified=0, the
    function must actually read the flag and rerun the read-only verifier -- not just return
    because the table happens to exist."""
    c = get_connection(str(tmp_path / "existing-unverified.db"))
    init_schema(c)
    migrate_persona_prompt_model(c)
    prompt_id = set_active_prompt(c, "p", "gpt-5.4")
    entry_id = _seed_entry(c)
    _insert_commentary(c, entry_id, prompt_id, created_at="2026-01-01T00:00:00+00:00")
    c.execute(
        "CREATE TABLE db_bootstrap_state (id INTEGER PRIMARY KEY CHECK (id = 1), "
        "is_fresh_install INTEGER NOT NULL, analyst_seeded INTEGER NOT NULL DEFAULT 0, "
        "current_result_selection_verified INTEGER NOT NULL DEFAULT 0, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    c.execute(
        "INSERT INTO db_bootstrap_state (id, is_fresh_install, analyst_seeded, "
        "current_result_selection_verified) VALUES (1, 0, 1, 0)"
    )

    migrate_bootstrap_state(c)

    row = c.execute("SELECT * FROM db_bootstrap_state WHERE id = 1").fetchone()
    assert row["current_result_selection_verified"] == 1
    c.close()


def test_migrate_bootstrap_state_leaves_unverified_flag_unset_on_an_ambiguous_existing_row(tmp_path):
    """On failure the flag stays 0 and nothing else changes -- the caller must retry after
    resolving the conflict, never silently proceed."""
    c = get_connection(str(tmp_path / "existing-unverified-ambiguous.db"))
    init_schema(c)
    migrate_persona_prompt_model(c)
    prompt_id = set_active_prompt(c, "p", "gpt-5.4")
    entry_id = _seed_entry(c)
    same_ts = "2026-01-01T00:00:00+00:00"
    _insert_commentary(c, entry_id, prompt_id, created_at=same_ts)
    _insert_commentary(c, entry_id, prompt_id, created_at=same_ts)  # tie
    c.execute(
        "CREATE TABLE db_bootstrap_state (id INTEGER PRIMARY KEY CHECK (id = 1), "
        "is_fresh_install INTEGER NOT NULL, analyst_seeded INTEGER NOT NULL DEFAULT 0, "
        "current_result_selection_verified INTEGER NOT NULL DEFAULT 0, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    c.execute(
        "INSERT INTO db_bootstrap_state (id, is_fresh_install, analyst_seeded, "
        "current_result_selection_verified) VALUES (1, 0, 1, 0)"
    )

    with pytest.raises(CurrentResultSelectionAmbiguousError):
        migrate_bootstrap_state(c)

    row = c.execute("SELECT * FROM db_bootstrap_state WHERE id = 1").fetchone()
    assert row["current_result_selection_verified"] == 0
    assert c.execute("SELECT COUNT(*) AS n FROM entry_commentary").fetchone()["n"] == 2
    c.close()


def test_verified_database_tolerates_a_new_result_with_higher_id_and_lower_timestamp(tmp_path):
    """Regression test: once a database is marked verified, a NEW v0.2-only result (which uses
    highest-ID selection and may legitimately have a LOWER wall-clock timestamp than an older row
    after a clock rollback) must never be compared against the retired v0.1 rule again -- the
    verifier must not rerun, so this can never break the next initialization/startup."""
    db_path = str(tmp_path / "post-verification.db")
    c = get_connection(db_path)
    init_schema(c)
    migrate_persona_prompt_model(c)
    prompt_id = set_active_prompt(c, "p", "gpt-5.4")
    entry_id = _seed_entry(c)
    _insert_commentary(c, entry_id, prompt_id, created_at="2026-01-01T00:00:00+00:00")

    migrate_bootstrap_state(c)  # verifies (compatible so far) and marks verified=1

    # A new v0.2 row: higher id, but an EARLIER created_at (clock rollback) -- would be a
    # "mismatch" under the retired v0.1 rule if it were ever re-checked.
    _insert_commentary(c, entry_id, prompt_id, created_at="2020-01-01T00:00:00+00:00")

    migrate_bootstrap_state(c)  # must NOT re-verify or raise -- already marked verified
    row = c.execute("SELECT * FROM db_bootstrap_state WHERE id = 1").fetchone()
    assert row["current_result_selection_verified"] == 1
    c.close()


def test_initialize_database_composes_the_full_sequence_for_a_fresh_database(tmp_path):
    c = get_connection(str(tmp_path / "init-fresh.db"))
    initialize_database(c)

    columns = {row["name"] for row in c.execute("PRAGMA table_info(persona_prompt)")}
    assert {"model", "preset_key"} <= columns
    rows = c.execute("SELECT * FROM persona_prompt").fetchall()
    assert len(rows) == 1
    assert rows[0]["preset_key"] == "analyst"
    bootstrap = c.execute("SELECT * FROM db_bootstrap_state WHERE id = 1").fetchone()
    assert bootstrap["is_fresh_install"] == 1
    assert bootstrap["current_result_selection_verified"] == 1
    c.close()


def test_initialize_upgrade_database_migrates_without_seeding_an_upgrade_database(tmp_path):
    c = get_connection(str(tmp_path / "init-upgrade.db"))
    init_schema(c)  # persona_prompt exists first, as if from an earlier release

    initialize_upgrade_database(c)

    columns = {row["name"] for row in c.execute("PRAGMA table_info(persona_prompt)")}
    assert {"model", "preset_key"} <= columns
    assert c.execute("SELECT COUNT(*) AS n FROM persona_prompt").fetchone()["n"] == 0
    bootstrap = c.execute("SELECT * FROM db_bootstrap_state WHERE id = 1").fetchone()
    assert bootstrap["is_fresh_install"] == 0
    assert bootstrap["current_result_selection_verified"] == 1
    c.close()


def test_verify_current_result_selection_compatibility_reports_multiple_conflicts_in_order(conn):
    e1 = _seed_entry(conn, title="e1", entry_date="2026-01-01")
    e2 = _seed_entry(conn, title="e2", entry_date="2026-01-02")
    prompt_id = set_active_prompt(conn, "p", "gpt-5.4")
    same_ts = "2026-01-01T00:00:00+00:00"
    ids_e2 = sorted([
        _insert_commentary(conn, e2, prompt_id, created_at=same_ts),
        _insert_commentary(conn, e2, prompt_id, created_at=same_ts),
    ])
    ids_e1 = sorted([
        _insert_commentary(conn, e1, prompt_id, created_at=same_ts),
        _insert_commentary(conn, e1, prompt_id, created_at=same_ts),
    ])

    with pytest.raises(CurrentResultSelectionAmbiguousError) as exc_info:
        verify_current_result_selection_compatibility(conn)

    conflicts = exc_info.value.conflicts
    assert [c["target"] for c in conflicts] == [f"entry_commentary:{e1}", f"entry_commentary:{e2}"]
    assert conflicts[0]["ids"] == ids_e1
    assert conflicts[1]["ids"] == ids_e2


# ---------------------------------------------------------------------------
# Analyst seeding (seed_analyst_prompt_if_fresh_install)
# ---------------------------------------------------------------------------

def test_seed_analyst_prompt_if_fresh_install_seeds_exactly_one_expected_row(conn):
    seed_analyst_prompt_if_fresh_install(conn)

    rows = conn.execute("SELECT * FROM persona_prompt").fetchall()
    assert len(rows) == 1
    row = rows[0]
    analyst = get_preset(DEFAULT_PERSPECTIVE_KEY)
    assert row["body_text"] == analyst.prompt
    assert row["preset_key"] == "analyst"
    assert row["version_no"] == 1
    assert row["is_active"] == 1
    from unflincher.db import DEFAULT_MODEL
    assert row["model"] == DEFAULT_MODEL


def test_seed_analyst_prompt_if_fresh_install_is_a_noop_without_a_prior_bootstrap_call(tmp_path):
    # A caller bug (forgetting migrate_bootstrap_state) must never silently seed a prompt anyway
    # -- db_bootstrap_state simply does not exist yet, so the row lookup returns None.
    c = get_connection(str(tmp_path / "no-bootstrap.db"))
    init_schema(c)
    migrate_persona_prompt_model(c)
    migrate_persona_prompt_preset_key(c)

    seed_analyst_prompt_if_fresh_install(c)

    assert c.execute("SELECT COUNT(*) AS n FROM persona_prompt").fetchone()["n"] == 0
    c.close()


def test_seed_analyst_prompt_if_fresh_install_is_a_noop_on_an_upgraded_database(tmp_path):
    c = get_connection(str(tmp_path / "upgrade-noop.db"))
    init_schema(c)  # persona_prompt exists first, as if from an earlier release
    migrate_bootstrap_state(c)  # classifies as upgrade, analyst_seeded forced to 1
    migrate_persona_prompt_model(c)
    migrate_persona_prompt_preset_key(c)

    seed_analyst_prompt_if_fresh_install(c)

    assert c.execute("SELECT COUNT(*) AS n FROM persona_prompt").fetchone()["n"] == 0
    c.close()


def test_seed_analyst_prompt_if_fresh_install_is_idempotent_across_repeated_calls(conn):
    seed_analyst_prompt_if_fresh_install(conn)
    seed_analyst_prompt_if_fresh_install(conn)
    seed_analyst_prompt_if_fresh_install(conn)

    assert conn.execute("SELECT COUNT(*) AS n FROM persona_prompt").fetchone()["n"] == 1


# ---------------------------------------------------------------------------
# Historical join aliases (prompt_version_no / prompt_preset_key)
# ---------------------------------------------------------------------------

def test_get_current_commentary_exposes_prompt_version_and_preset_key(conn):
    entry_id = _seed_entry(conn)
    analyst = get_preset("analyst")
    prompt_id = set_active_prompt(conn, analyst.prompt, "gpt-5.4")
    _insert_commentary(conn, entry_id, prompt_id, created_at="2026-01-01T00:00:00+00:00")

    current = get_current_commentary(conn, entry_id)

    assert current["prompt_version_no"] == 1
    assert current["prompt_preset_key"] == "analyst"


def test_get_current_report_exposes_prompt_version_and_preset_key(conn):
    coach = get_preset("coach")
    prompt_id = set_active_prompt(conn, coach.prompt, "gpt-5.4")
    _insert_report(conn, prompt_id, created_at="2026-01-01T00:00:00+00:00")

    current = get_current_report(conn)

    assert current["prompt_version_no"] == 1
    assert current["prompt_preset_key"] == "coach"


def test_report_version_queries_also_expose_the_join_aliases(conn):
    prompt_id = set_active_prompt(conn, "custom text", "gpt-5.4")
    report_id = _insert_report(conn, prompt_id, created_at="2026-01-01T00:00:00+00:00")

    report_versions = list_report_versions(conn)
    single_report = get_report_by_id(conn, report_id)

    for row in (*report_versions, single_report):
        assert row["prompt_version_no"] == 1
        assert row["prompt_preset_key"] is None  # "custom text" matches no shipped preset


def test_existing_migrated_rows_display_custom_even_when_body_matches_a_current_preset(conn):
    # A pre-existing row whose body happens to equal the CURRENT Analyst preset text (e.g. an old
    # custom persona that coincidentally matches, or a database that predates preset_key and was
    # migrated) must still show as Custom in the historical join -- preset_key is backfilled NULL
    # by the migration, never retrospectively classified from body content (item 6).
    analyst = get_preset("analyst")
    entry_id = _seed_entry(conn)
    conn.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active, created_at) "
        "VALUES (1, ?, 'gpt-5.4', 1, '2025-01-01T00:00:00+00:00')",
        (analyst.prompt,),
    )
    prompt_id = conn.execute("SELECT id FROM persona_prompt WHERE version_no = 1").fetchone()["id"]
    migrate_persona_prompt_preset_key(conn)  # backfills preset_key IS NULL, does not classify
    _insert_commentary(conn, entry_id, prompt_id, created_at="2026-01-01T00:00:00+00:00")

    current = get_current_commentary(conn, entry_id)
    assert current["prompt_preset_key"] is None


# ---------------------------------------------------------------------------
# Current-result-selection compatibility verifier
# ---------------------------------------------------------------------------

def test_verify_current_result_selection_compatibility_passes_on_a_clean_database(conn):
    entry_id = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "p", "gpt-5.4")
    _insert_commentary(conn, entry_id, prompt_id, created_at="2026-01-01T00:00:00+00:00")
    _insert_commentary(conn, entry_id, prompt_id, created_at="2026-01-02T00:00:00+00:00")
    _insert_report(conn, prompt_id, created_at="2026-01-01T00:00:00+00:00")

    verify_current_result_selection_compatibility(conn)  # must not raise


def test_verify_current_result_selection_compatibility_is_a_noop_on_an_empty_database(conn):
    verify_current_result_selection_compatibility(conn)  # must not raise; nothing to check


def test_verify_current_result_selection_compatibility_rejects_tied_max_created_at_for_an_entry(conn):
    entry_id = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "p", "gpt-5.4")
    same_ts = "2026-01-01T00:00:00+00:00"
    id1 = _insert_commentary(conn, entry_id, prompt_id, created_at=same_ts)
    id2 = _insert_commentary(conn, entry_id, prompt_id, created_at=same_ts)

    with pytest.raises(CurrentResultSelectionAmbiguousError) as exc_info:
        verify_current_result_selection_compatibility(conn)

    conflict = exc_info.value.conflicts[0]
    assert conflict["target"] == f"entry_commentary:{entry_id}"
    assert conflict["reason"] == "tied_max_created_at"
    assert conflict["ids"] == sorted([id1, id2])
    # No data was changed by the failed check.
    assert conn.execute("SELECT COUNT(*) AS n FROM entry_commentary").fetchone()["n"] == 2


def test_verify_current_result_selection_compatibility_rejects_tied_max_created_at_for_the_report(conn):
    prompt_id = set_active_prompt(conn, "p", "gpt-5.4")
    same_ts = "2026-01-01T00:00:00+00:00"
    id1 = _insert_report(conn, prompt_id, created_at=same_ts)
    id2 = _insert_report(conn, prompt_id, created_at=same_ts)

    with pytest.raises(CurrentResultSelectionAmbiguousError) as exc_info:
        verify_current_result_selection_compatibility(conn)

    conflict = exc_info.value.conflicts[0]
    assert conflict["target"] == "aggregate_report"
    assert conflict["reason"] == "tied_max_created_at"
    assert conflict["ids"] == sorted([id1, id2])


def test_verify_current_result_selection_compatibility_rejects_a_mismatch(conn):
    # A backdated row: the HIGHEST id has an EARLIER created_at than a lower id -- v0.1's
    # created_at-based rule would pick the lower id, but the new highest-ID rule would pick a
    # different one. The two rules disagree, so this must raise rather than silently switch.
    entry_id = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "p", "gpt-5.4")
    older_but_higher_id = _insert_commentary(conn, entry_id, prompt_id, created_at="2020-01-01T00:00:00+00:00")
    newer_id_older_timestamp = _insert_commentary(
        conn, entry_id, prompt_id, created_at="2019-01-01T00:00:00+00:00"
    )
    assert newer_id_older_timestamp > older_but_higher_id  # sanity: ids still increase with insertion

    with pytest.raises(CurrentResultSelectionAmbiguousError) as exc_info:
        verify_current_result_selection_compatibility(conn)

    conflict = exc_info.value.conflicts[0]
    assert conflict["target"] == f"entry_commentary:{entry_id}"
    assert conflict["reason"] == "mismatch"
    assert conflict["v01_selected_id"] == older_but_higher_id  # earliest INSERT, but LATEST created_at
    assert conflict["highest_id"] == newer_id_older_timestamp


def test_verify_current_result_selection_compatibility_is_idempotent_and_read_only(conn):
    entry_id = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "p", "gpt-5.4")
    _insert_commentary(conn, entry_id, prompt_id, created_at="2026-01-01T00:00:00+00:00")
    before = [dict(r) for r in conn.execute("SELECT * FROM entry_commentary ORDER BY id").fetchall()]

    verify_current_result_selection_compatibility(conn)
    verify_current_result_selection_compatibility(conn)  # rerun -- still passes, changes nothing

    after = [dict(r) for r in conn.execute("SELECT * FROM entry_commentary ORDER BY id").fetchall()]
    assert before == after


def test_verify_current_result_selection_compatibility_ignores_failed_rows(conn):
    # A 'failed' row must never participate in tie/mismatch detection -- only 'ok' rows are a
    # target's candidate "current" results.
    entry_id = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "p", "gpt-5.4")
    same_ts = "2026-01-01T00:00:00+00:00"
    _insert_commentary(conn, entry_id, prompt_id, created_at=same_ts, status="ok")
    _insert_commentary(conn, entry_id, prompt_id, created_at=same_ts, status="failed")

    verify_current_result_selection_compatibility(conn)  # must not raise: only one 'ok' row


# ---------------------------------------------------------------------------
# Admission baseline / current-result-selection consistency (item 11)
# ---------------------------------------------------------------------------

def test_current_commentary_selection_matches_the_admission_baseline_rule(conn):
    """get_current_commentary (the user-facing 'current' selection) and
    _current_result_id_for_target (the admission-time baseline retry/recovery compare against)
    must always agree on which result is current -- both use the highest successful ID."""
    from unflincher.db import _current_result_id_for_target

    entry_id = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "p", "gpt-5.4")
    _insert_commentary(conn, entry_id, prompt_id, created_at="2026-01-01T00:00:00+00:00")
    newest_id = _insert_commentary(conn, entry_id, prompt_id, created_at="2026-01-02T00:00:00+00:00")

    current = get_current_commentary(conn, entry_id)
    baseline = _current_result_id_for_target(
        conn, PreparedRegenTarget("entry_commentary", entry_id, 1, "fp")
    )

    assert current["id"] == newest_id
    assert baseline == newest_id == current["id"]


def test_current_report_selection_matches_the_admission_baseline_rule(conn):
    from unflincher.db import _current_result_id_for_target

    prompt_id = set_active_prompt(conn, "p", "gpt-5.4")
    _insert_report(conn, prompt_id, created_at="2026-01-01T00:00:00+00:00")
    newest_id = _insert_report(conn, prompt_id, created_at="2026-01-02T00:00:00+00:00")

    current = get_current_report(conn)
    baseline = _current_result_id_for_target(
        conn, PreparedRegenTarget("aggregate_report", None, 1, "fp")
    )

    assert current["id"] == newest_id
    assert baseline == newest_id == current["id"]


def test_current_commentary_uses_highest_successful_id_after_clock_rollback(conn):
    entry_id = _seed_entry(conn)
    prompt_id = set_active_prompt(conn, "p", "gpt-5.4")
    _insert_commentary(conn, entry_id, prompt_id, created_at="2026-01-02T00:00:00+00:00")
    highest_id = _insert_commentary(
        conn, entry_id, prompt_id, created_at="2020-01-01T00:00:00+00:00"
    )

    current = get_current_commentary(conn, entry_id)

    assert current["id"] == highest_id


def test_current_report_uses_highest_successful_id_after_clock_rollback(conn):
    prompt_id = set_active_prompt(conn, "p", "gpt-5.4")
    _insert_report(conn, prompt_id, created_at="2026-01-02T00:00:00+00:00")
    highest_id = _insert_report(
        conn, prompt_id, created_at="2020-01-01T00:00:00+00:00"
    )

    current = get_current_report(conn)

    assert current["id"] == highest_id


# ---------------------------------------------------------------------------
# Atomic apply-all rollback with preset (item 7)
# ---------------------------------------------------------------------------

def _enqueue_with_activation(conn, *, entry_id, body_text, model, activate_preset_key=None):
    from unflincher.db import enqueue_snapshot_regen_job, get_ordered_entry_ids

    preflight_entry_ids = get_ordered_entry_ids(conn)
    targets = [PreparedRegenTarget("entry_commentary", entry_id, 1, "fp-for-activation-test")]
    return enqueue_snapshot_regen_job(
        conn, activate_prompt=(body_text, model), activate_preset_key=activate_preset_key,
        preflight_entry_ids=preflight_entry_ids, targets=targets, owner_token="test-owner",
    )


def test_enqueue_snapshot_regen_job_activate_prompt_persists_preset_key_on_success(conn):
    entry_id = _seed_entry(conn)
    analyst = get_preset("analyst")
    job_id, activated_prompt_id = _enqueue_with_activation(
        conn, entry_id=entry_id, body_text=analyst.prompt, model="gpt-5.4",
    )

    active = get_active_prompt(conn)
    assert active["id"] == activated_prompt_id
    assert active["preset_key"] == "analyst"
    assert job_id is not None


def test_enqueue_snapshot_regen_job_ignores_a_forged_activate_preset_key_hint(conn):
    """activate_preset_key is a caller-claimed hint only -- a forged claim can never override the
    server's own exact-text classification (see db._insert_activated_persona_prompt_version)."""
    entry_id = _seed_entry(conn)
    analyst = get_preset("analyst")
    _job_id, activated_prompt_id = _enqueue_with_activation(
        conn, entry_id=entry_id, body_text=analyst.prompt, model="gpt-5.4",
        activate_preset_key="coach",
    )

    active = get_active_prompt(conn)
    assert active["id"] == activated_prompt_id
    assert active["preset_key"] == "analyst"


def test_enqueue_snapshot_regen_job_activate_prompt_rolls_back_preset_key_on_target_busy(conn):
    """If apply-all rolls back because of a busy target lease, NO prompt row (and therefore no
    preset key) may persist -- the whole activate+enqueue transaction is all-or-nothing. Also
    proves this holds even when a (forged) activate_preset_key hint was given."""
    entry_id = _seed_entry(conn)
    analyst = get_preset("analyst")
    # Pre-hold the entry's target lease so the enqueue's own lease acquisition fails.
    from unflincher.db import acquire_lease, entry_target_key
    acquire_lease(conn, entry_target_key(entry_id), "direct", "someone-else")

    rows_before = conn.execute("SELECT COUNT(*) AS n FROM persona_prompt").fetchone()["n"]

    with pytest.raises(TargetBusyError):
        _enqueue_with_activation(
            conn, entry_id=entry_id, body_text=analyst.prompt, model="gpt-5.4",
            activate_preset_key="coach",
        )

    rows_after = conn.execute("SELECT COUNT(*) AS n FROM persona_prompt").fetchone()["n"]
    assert rows_after == rows_before  # no prompt row (and so no preset key) was ever persisted
    assert get_active_prompt(conn) is None


def test_enqueue_snapshot_regen_job_activate_prompt_rolls_back_preset_key_when_maintenance_locked(conn):
    entry_id = _seed_entry(conn)
    analyst = get_preset("analyst")
    set_maintenance_locked(conn, True)

    rows_before = conn.execute("SELECT COUNT(*) AS n FROM persona_prompt").fetchone()["n"]

    from unflincher.db import MaintenanceLockedError
    with pytest.raises(MaintenanceLockedError):
        _enqueue_with_activation(conn, entry_id=entry_id, body_text=analyst.prompt, model="gpt-5.4")

    rows_after = conn.execute("SELECT COUNT(*) AS n FROM persona_prompt").fetchone()["n"]
    assert rows_after == rows_before
