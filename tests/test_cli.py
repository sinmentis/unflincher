import hashlib
import json
import sqlite3
import subprocess
import sys

import openpyxl
import pytest


def _make_workbook(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "日记"
    ws.append(["标题", "链接", "创建时间", "修改时间", "内容"])
    ws.append(["t", "https://x", "2026-01-01 00:00:00", "2026-01-01 00:00:00", "<p>hi</p>"])
    wb.save(path)


def test_cli_import_reports_count(tmp_path):
    xlsx_path = tmp_path / "export.xlsx"
    db_path = tmp_path / "unflincher.db"
    _make_workbook(xlsx_path)

    result = subprocess.run(
        [sys.executable, "-m", "unflincher.cli", "import", "--excel", str(xlsx_path), "--db", str(db_path)],
        capture_output=True, text=True,
    )

    assert result.returncode == 0
    assert "Imported 1 " in result.stdout or "imported 1 " in result.stdout.lower()


def test_cli_import_fully_migrates_and_seeds_analyst_on_a_fresh_database(tmp_path):
    """Regression test: the CLI import bootstrap must use the same db.initialize_database()
    interface as app startup -- a fresh database must end up fully migrated (persona_prompt.model
    present, not just preset_key) and seeded with exactly one Analyst prompt, not left partially
    initialized."""
    import sqlite3

    xlsx_path = tmp_path / "export.xlsx"
    db_path = tmp_path / "unflincher.db"
    _make_workbook(xlsx_path)

    result = subprocess.run(
        [sys.executable, "-m", "unflincher.cli", "import", "--excel", str(xlsx_path), "--db", str(db_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(persona_prompt)")}
    assert "model" in columns
    assert "preset_key" in columns
    rows = conn.execute("SELECT * FROM persona_prompt").fetchall()
    assert len(rows) == 1
    assert rows[0]["preset_key"] == "analyst"
    assert rows[0]["is_active"] == 1
    assert rows[0]["model"] is not None
    conn.close()


def test_cli_import_requires_offline_bootstrap_for_a_pre_existing_database(tmp_path):
    """Import must not bypass the fail-locked v0.1 upgrade path."""
    import sqlite3

    xlsx_path = tmp_path / "export.xlsx"
    db_path = tmp_path / "unflincher.db"
    _make_workbook(xlsx_path)

    seed = sqlite3.connect(db_path)
    seed.execute(
        "CREATE TABLE persona_prompt (id INTEGER PRIMARY KEY AUTOINCREMENT, version_no INTEGER "
        "NOT NULL, body_text TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 0, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    seed.commit()
    seed.close()

    result = subprocess.run(
        [sys.executable, "-m", "unflincher.cli", "import", "--excel", str(xlsx_path), "--db", str(db_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "offline bootstrap required" in result.stderr


def test_cli_import_missing_columns_exits_nonzero(tmp_path):
    xlsx_path = tmp_path / "bad.xlsx"
    db_path = tmp_path / "unflincher.db"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "日记"
    ws.append(["标题", "内容"])
    ws.append(["t", "<p>hi</p>"])
    wb.save(xlsx_path)

    result = subprocess.run(
        [sys.executable, "-m", "unflincher.cli", "import", "--excel", str(xlsx_path), "--db", str(db_path)],
        capture_output=True, text=True,
    )


    assert result.returncode != 0
    assert "链接" in result.stderr
    assert "创建时间" in result.stderr


def _write_upgrade_database(path, *, running_job=False):
    from unflincher.db import (
        V01_RELEASE_SCHEMA,
        migrate_chat_session,
        migrate_persona_prompt_model,
    )

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(V01_RELEASE_SCHEMA)
    migrate_persona_prompt_model(conn)
    migrate_chat_session(conn)
    prompt_bodies = {
        "inactive": "Earlier private instructions must also remain byte-identical.",
        "active": "Keep the owner's exact private instructions unchanged.",
    }
    conn.execute(
        "INSERT INTO persona_prompt "
        "(version_no, body_text, model, is_active, created_at) "
        "VALUES (6, ?, 'claude-sonnet-4.6', 0, '2026-06-01T01:02:03+00:00')",
        (prompt_bodies["inactive"],),
    )
    active_prompt_id = conn.execute(
        "INSERT INTO persona_prompt "
        "(version_no, body_text, model, is_active, created_at) "
        "VALUES (7, ?, 'gpt-5.4', 1, '2026-07-01T02:03:04+00:00')",
        (prompt_bodies["active"],),
    ).lastrowid
    if running_job:
        conn.execute(
            "INSERT INTO regen_job (prompt_version_id, status) VALUES (?, 'running')",
            (active_prompt_id,),
        )
    conn.commit()
    conn.close()
    return prompt_bodies


def _run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "unflincher.cli", *args],
        capture_output=True,
        text=True,
    )


def test_cli_bootstrap_upgrades_existing_database_preserves_prompt_and_locks(tmp_path):
    db_path = tmp_path / "upgrade.db"
    prompt_bodies = _write_upgrade_database(db_path)

    result = _run_cli("bootstrap", "--db", str(db_path), "--json")

    assert result.returncode == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["bootstrap_state"] == {
        "analyst_seeded": True,
        "current_result_selection_verified": True,
        "is_fresh_install": False,
    }
    assert payload["maintenance"] == {
        "active_lease_count": 0,
        "locked": True,
        "running_regen_job_ids": [],
    }
    assert payload["active_prompt"] == {
        "body_sha256": hashlib.sha256(
            prompt_bodies["active"].encode("utf-8")
        ).hexdigest(),
        "created_at": "2026-07-01T02:03:04+00:00",
        "id": 2,
        "is_active": 1,
        "model": "gpt-5.4",
        "preset_key": None,
        "version_no": 7,
    }

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    prompts = conn.execute("SELECT * FROM persona_prompt ORDER BY id").fetchall()
    assert len(prompts) == 2
    assert prompts[0]["id"] == 1
    assert prompts[0]["version_no"] == 6
    assert prompts[0]["body_text"] == prompt_bodies["inactive"]
    assert prompts[0]["model"] == "claude-sonnet-4.6"
    assert prompts[0]["is_active"] == 0
    assert prompts[0]["created_at"] == "2026-06-01T01:02:03+00:00"
    assert prompts[0]["preset_key"] is None
    assert prompts[1]["id"] == 2
    assert prompts[1]["version_no"] == 7
    assert prompts[1]["body_text"] == prompt_bodies["active"]
    assert prompts[1]["model"] == "gpt-5.4"
    assert prompts[1]["is_active"] == 1
    assert prompts[1]["created_at"] == "2026-07-01T02:03:04+00:00"
    assert prompts[1]["preset_key"] is None
    conn.close()


def test_cli_bootstrap_is_idempotent(tmp_path):
    db_path = tmp_path / "upgrade.db"
    _write_upgrade_database(db_path)

    first = _run_cli("bootstrap", "--db", str(db_path), "--json")
    second = _run_cli("bootstrap", "--db", str(db_path), "--json")

    assert first.returncode == second.returncode == 0
    assert json.loads(first.stdout) == json.loads(second.stdout)


@pytest.mark.parametrize(
    "statements",
    [
        ("ALTER TABLE regen_job ADD COLUMN snapshot_entry_count INTEGER",),
        (
            "ALTER TABLE regen_job ADD COLUMN snapshot_entry_count INTEGER",
            "ALTER TABLE regen_job_item ADD COLUMN request_format_version INTEGER",
        ),
        (
            "ALTER TABLE regen_job ADD COLUMN snapshot_entry_count INTEGER",
            "ALTER TABLE regen_job_item ADD COLUMN request_format_version INTEGER",
            "ALTER TABLE regen_job_item ADD COLUMN request_fingerprint TEXT",
        ),
        (
            "ALTER TABLE regen_job ADD COLUMN snapshot_entry_count INTEGER",
            "ALTER TABLE regen_job_item ADD COLUMN request_format_version INTEGER",
            "ALTER TABLE regen_job_item ADD COLUMN request_fingerprint TEXT",
            "ALTER TABLE regen_job_item ADD COLUMN baseline_result_id INTEGER",
        ),
    ],
    ids=[
        "snapshot-count",
        "request-format-version",
        "request-fingerprint",
        "baseline-result-id",
    ],
)
def test_cli_bootstrap_resumes_after_each_generation_safety_column(
    tmp_path,
    statements,
):
    from unflincher.db import (
        init_schema,
        lock_maintenance_for_bootstrap,
        migrate_bootstrap_state,
        migrate_persona_prompt_preset_key,
    )

    db_path = tmp_path / "interrupted-columns.db"
    _write_upgrade_database(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    lock_maintenance_for_bootstrap(conn)
    migrate_bootstrap_state(conn)
    init_schema(conn)
    migrate_persona_prompt_preset_key(conn)
    for statement in statements:
        conn.execute(statement)
    conn.close()

    result = _run_cli("bootstrap", "--db", str(db_path), "--json")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["maintenance"]["locked"] is True


def test_cli_bootstrap_resumes_after_snapshot_table_creation_before_index(tmp_path):
    from unflincher.db import (
        SCHEMA,
        lock_maintenance_for_bootstrap,
        migrate_bootstrap_state,
    )

    db_path = tmp_path / "interrupted-snapshot-index.db"
    _write_upgrade_database(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    lock_maintenance_for_bootstrap(conn)
    migrate_bootstrap_state(conn)
    snapshot_index_statement = (
        "CREATE INDEX IF NOT EXISTS ix_regen_job_entry_snapshot_job "
        "ON regen_job_entry_snapshot (job_id);"
    )
    partial_schema, separator, remainder = SCHEMA.partition(snapshot_index_statement)
    assert separator == snapshot_index_statement
    assert remainder.strip() == ""
    conn.executescript(partial_schema)
    conn.close()

    result = _run_cli("bootstrap", "--db", str(db_path), "--json")

    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(db_path)
    indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(regen_job_entry_snapshot)")
    }
    assert "ix_regen_job_entry_snapshot_job" in indexes
    conn.close()


def test_cli_bootstrap_refuses_missing_database_without_creating_it(tmp_path):
    db_path = tmp_path / "missing.db"

    result = _run_cli("bootstrap", "--db", str(db_path), "--json")

    assert result.returncode == 1
    assert "cannot open existing database" in result.stderr
    assert not db_path.exists()


def test_cli_bootstrap_rejects_unrelated_sqlite_database_without_mutating_it(tmp_path):
    db_path = tmp_path / "unrelated.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE diary_entry (id INTEGER PRIMARY KEY)")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    conn.commit()
    conn.close()

    result = _run_cli("bootstrap", "--db", str(db_path), "--json")

    assert result.returncode == 1
    assert "does not match the released v0.1/v0.2 schema contract" in result.stderr
    conn = sqlite3.connect(db_path)
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert tables == {"diary_entry"}
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    conn.close()


def test_cli_bootstrap_rejects_v01_lookalike_with_wrong_unique_index(tmp_path):
    db_path = tmp_path / "wrong-index.db"
    _write_upgrade_database(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("DROP INDEX ux_persona_prompt_one_active")
    conn.execute(
        "CREATE INDEX ux_persona_prompt_one_active ON persona_prompt (is_active)"
    )
    conn.commit()
    conn.close()

    result = _run_cli("bootstrap", "--db", str(db_path), "--json")

    assert result.returncode == 1
    assert "persona_prompt.indexes" in result.stderr
    conn = sqlite3.connect(db_path)
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "maintenance_control" not in tables
    conn.close()


def test_cli_bootstrap_rejects_literal_changing_check_constraint(tmp_path):
    from unflincher.db import V01_EFFECTIVE_SCHEMA

    db_path = tmp_path / "wrong-check.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        V01_EFFECTIVE_SCHEMA.replace(
            "CHECK (status IN ('running', 'done', 'cancelled'))",
            "CHECK (status IN ('RUNNING', 'done', 'cancelled'))",
        )
    )
    conn.commit()
    conn.close()

    result = _run_cli("bootstrap", "--db", str(db_path), "--json")

    assert result.returncode == 1
    assert "regen_job.sql" in result.stderr


def test_cli_bootstrap_rejects_persistent_trigger(tmp_path):
    db_path = tmp_path / "trigger.db"
    _write_upgrade_database(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TRIGGER destructive_prompt_trigger "
        "AFTER INSERT ON persona_prompt "
        "BEGIN DELETE FROM diary_entry; END"
    )
    conn.commit()
    conn.close()

    result = _run_cli("bootstrap", "--db", str(db_path), "--json")

    assert result.returncode == 1
    assert "unexpected_objects=['trigger:destructive_prompt_trigger']" in result.stderr
    conn = sqlite3.connect(db_path)
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "maintenance_control" not in tables
    conn.close()


def test_cli_bootstrap_rejects_running_regeneration_job_before_any_migration(tmp_path):
    db_path = tmp_path / "running.db"
    _write_upgrade_database(db_path, running_job=True)

    result = _run_cli("bootstrap", "--db", str(db_path), "--json")

    assert result.returncode == 1
    assert "running regeneration job" in result.stderr.lower()
    conn = sqlite3.connect(db_path)
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "db_bootstrap_state" not in tables
    assert "maintenance_control" not in tables
    conn.close()


def test_cli_bootstrap_rerun_rejects_active_generation_lease_without_relocking(
    tmp_path,
):
    db_path = tmp_path / "active-lease.db"
    _write_upgrade_database(db_path)
    assert _run_cli("bootstrap", "--db", str(db_path), "--json").returncode == 0
    assert _run_cli(
        "maintenance",
        "unlock",
        "--db",
        str(db_path),
        "--confirm-service-healthy",
        "--json",
    ).returncode == 0
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO generation_lease (target_key, lease_kind, owner_token) "
        "VALUES ('entry:1', 'direct', 'owner')"
    )
    conn.commit()
    conn.close()

    result = _run_cli("bootstrap", "--db", str(db_path), "--json")

    assert result.returncode == 1
    assert "active generation lease(s): 1" in result.stderr
    conn = sqlite3.connect(db_path)
    assert conn.execute(
        "SELECT locked FROM maintenance_control WHERE id = 1"
    ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM generation_lease").fetchone()[0] == 1
    conn.close()


def test_cli_bootstrap_rechecks_unverified_state_before_locking(tmp_path):
    db_path = tmp_path / "unverified-ambiguous.db"
    _write_upgrade_database(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE db_bootstrap_state ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), "
        "is_fresh_install INTEGER NOT NULL, "
        "analyst_seeded INTEGER NOT NULL DEFAULT 0, "
        "current_result_selection_verified INTEGER NOT NULL DEFAULT 0, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    conn.execute(
        "INSERT INTO db_bootstrap_state "
        "(id, is_fresh_install, analyst_seeded, current_result_selection_verified) "
        "VALUES (1, 0, 1, 0)"
    )
    entry_id = conn.execute(
        "INSERT INTO diary_entry "
        "(title, content_html_raw, content_html, content_text, entry_date, source) "
        "VALUES ('e', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    for _ in range(2):
        conn.execute(
            "INSERT INTO entry_commentary "
            "(entry_id, prompt_version_id, model, body_text, status, created_at) "
            "VALUES (?, 2, 'gpt-5.4', 'x', 'ok', '2026-01-01T00:00:00+00:00')",
            (entry_id,),
        )
    conn.commit()
    conn.close()

    result = _run_cli("bootstrap", "--db", str(db_path), "--json")

    assert result.returncode == 1
    assert "current-result selection compatibility check failed" in result.stderr
    conn = sqlite3.connect(db_path)
    assert conn.execute(
        "SELECT current_result_selection_verified FROM db_bootstrap_state WHERE id = 1"
    ).fetchone()[0] == 0
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "maintenance_control" not in tables
    conn.close()


def test_bootstrap_failure_after_preflight_leaves_maintenance_locked(
    tmp_path,
    monkeypatch,
):
    import unflincher.cli as cli_module

    db_path = tmp_path / "failed-migration.db"
    _write_upgrade_database(db_path)

    def fail_after_lock(_conn):
        raise RuntimeError("simulated migration failure")

    monkeypatch.setattr(cli_module, "initialize_upgrade_database", fail_after_lock)

    with pytest.raises(RuntimeError, match="simulated migration failure"):
        cli_module._run_bootstrap(str(db_path))

    conn = sqlite3.connect(db_path)
    assert conn.execute(
        "SELECT locked FROM maintenance_control WHERE id = 1"
    ).fetchone()[0] == 1
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "db_bootstrap_state" not in tables
    conn.close()


def test_bootstrap_detects_inactive_prompt_identity_change(tmp_path, monkeypatch):
    import unflincher.cli as cli_module

    db_path = tmp_path / "changed-inactive-prompt.db"
    _write_upgrade_database(db_path)
    real_initialize = cli_module.initialize_upgrade_database

    def mutate_inactive_prompt(conn):
        real_initialize(conn)
        conn.execute(
            "UPDATE persona_prompt SET body_text = 'changed' WHERE is_active = 0"
        )

    monkeypatch.setattr(cli_module, "initialize_upgrade_database", mutate_inactive_prompt)

    with pytest.raises(sqlite3.IntegrityError, match="immutable during bootstrap"):
        cli_module._run_bootstrap(str(db_path))

    conn = sqlite3.connect(db_path)
    assert conn.execute(
        "SELECT locked FROM maintenance_control WHERE id = 1"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT body_text FROM persona_prompt WHERE is_active = 0"
    ).fetchone()[0] == "Earlier private instructions must also remain byte-identical."
    conn.close()


def test_cli_bootstrap_rejects_ambiguous_current_results_before_bootstrap_state_write(
    tmp_path,
):
    from unflincher.db import (
        get_connection,
        init_schema,
        migrate_chat_session,
        migrate_persona_prompt_model,
        set_active_prompt,
    )

    db_path = tmp_path / "ambiguous.db"
    conn = get_connection(str(db_path))
    init_schema(conn)
    migrate_persona_prompt_model(conn)
    migrate_chat_session(conn)
    prompt_id = set_active_prompt(conn, "p", "gpt-5.4")
    entry_id = conn.execute(
        "INSERT INTO diary_entry "
        "(title, content_html_raw, content_html, content_text, entry_date, source) "
        "VALUES ('e', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid
    same_timestamp = "2026-01-01T00:00:00+00:00"
    conn.executemany(
        "INSERT INTO entry_commentary "
        "(entry_id, prompt_version_id, model, body_text, status, created_at) "
        "VALUES (?, ?, 'gpt-5.4', 'x', 'ok', ?)",
        [
            (entry_id, prompt_id, same_timestamp),
            (entry_id, prompt_id, same_timestamp),
        ],
    )
    conn.close()

    result = _run_cli("bootstrap", "--db", str(db_path), "--json")

    assert result.returncode == 1
    assert "current-result selection compatibility check failed" in result.stderr
    conn = sqlite3.connect(db_path)
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "db_bootstrap_state" not in tables
    assert conn.execute(
        "SELECT locked FROM maintenance_control WHERE id = 1"
    ).fetchone()[0] == 0
    conn.close()


def test_cli_maintenance_status_lock_and_confirmed_unlock(tmp_path):
    db_path = tmp_path / "maintenance.db"
    _write_upgrade_database(db_path)
    assert _run_cli("bootstrap", "--db", str(db_path), "--json").returncode == 0

    status = _run_cli("maintenance", "status", "--db", str(db_path), "--json")
    assert status.returncode == 0
    assert json.loads(status.stdout)["maintenance"]["locked"] is True

    unconfirmed = _run_cli("maintenance", "unlock", "--db", str(db_path), "--json")
    assert unconfirmed.returncode == 2

    unlocked = _run_cli(
        "maintenance",
        "unlock",
        "--db",
        str(db_path),
        "--confirm-service-healthy",
        "--json",
    )
    assert unlocked.returncode == 0
    assert json.loads(unlocked.stdout)["maintenance"]["locked"] is False

    locked = _run_cli("maintenance", "lock", "--db", str(db_path), "--json")
    assert locked.returncode == 0
    assert json.loads(locked.stdout)["maintenance"]["locked"] is True


def test_cli_maintenance_unlock_refuses_active_generation(tmp_path):
    db_path = tmp_path / "busy.db"
    _write_upgrade_database(db_path)
    assert _run_cli("bootstrap", "--db", str(db_path), "--json").returncode == 0
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO generation_lease (target_key, lease_kind, owner_token) "
        "VALUES ('entry:1', 'direct', 'owner')"
    )
    conn.commit()
    conn.close()

    result = _run_cli(
        "maintenance",
        "unlock",
        "--db",
        str(db_path),
        "--confirm-service-healthy",
        "--json",
    )

    assert result.returncode == 1
    assert "active generation" in result.stderr.lower()
    status = _run_cli("maintenance", "status", "--db", str(db_path), "--json")
    assert json.loads(status.stdout)["maintenance"]["locked"] is True


def test_cli_maintenance_lock_rejects_unbootstrapped_database_without_mutating_it(
    tmp_path,
):
    db_path = tmp_path / "v01.db"
    _write_upgrade_database(db_path)

    result = _run_cli("maintenance", "lock", "--db", str(db_path), "--json")

    assert result.returncode == 1
    assert "offline bootstrap has not completed" in result.stderr
    conn = sqlite3.connect(db_path)
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "maintenance_control" not in tables
    conn.close()


# ---------------------------------------------------------------------------
# Deployment probe subcommand (in-process: a real subprocess would need a real Copilot CLI/token)
# ---------------------------------------------------------------------------

def test_cli_probe_prints_reply_and_exits_zero(monkeypatch, capsys):
    import unflincher.cli as cli_module

    async def _fake_run_probe(model):
        assert model == "claude-sonnet-4.6"
        return "ok"

    shutdown_calls = []

    async def _fake_shutdown():
        shutdown_calls.append(True)

    monkeypatch.setattr("unflincher.probe.run_probe", _fake_run_probe)
    monkeypatch.setattr("unflincher.llm.shutdown_client", _fake_shutdown)

    exit_code = cli_module.main(["probe", "--model", "claude-sonnet-4.6"])

    assert exit_code == 0
    assert shutdown_calls == [True]  # client always torn down after a one-shot CLI invocation
    captured = capsys.readouterr()
    assert "ok" in captured.out
    assert "claude-sonnet-4.6" in captured.out


def test_cli_probe_defaults_to_configured_model(monkeypatch):
    import unflincher.cli as cli_module

    seen = {}

    async def _fake_run_probe(model):
        seen["model"] = model
        return "ok"

    async def _fake_shutdown():
        pass

    monkeypatch.setenv("UNFLINCHER_LLM_MODEL", "gpt-5.5")
    monkeypatch.setattr("unflincher.probe.run_probe", _fake_run_probe)
    monkeypatch.setattr("unflincher.llm.shutdown_client", _fake_shutdown)

    exit_code = cli_module.main(["probe"])

    assert exit_code == 0
    assert seen["model"] == "gpt-5.5"


def test_cli_probe_exits_nonzero_and_still_shuts_down_client_on_failure(monkeypatch, capsys):
    import unflincher.cli as cli_module

    async def _fake_run_probe(model):
        raise RuntimeError("model unavailable")

    shutdown_calls = []

    async def _fake_shutdown():
        shutdown_calls.append(True)

    monkeypatch.setattr("unflincher.probe.run_probe", _fake_run_probe)
    monkeypatch.setattr("unflincher.llm.shutdown_client", _fake_shutdown)

    exit_code = cli_module.main(["probe", "--model", "bad-model"])

    assert exit_code == 1
    assert shutdown_calls == [True]  # torn down even on failure
    captured = capsys.readouterr()
    assert "model unavailable" in captured.err


def test_cli_probe_never_touches_a_database_connection(monkeypatch):
    # The probe subcommand itself must never call get_connection()/init_schema() -- unlike the
    # "import" subcommand, no --db argument even exists for "probe".
    import unflincher.cli as cli_module

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("cli probe subcommand must never open a database connection")

    async def _fake_run_probe(model):
        return "ok"

    async def _fake_shutdown():
        pass

    monkeypatch.setattr(cli_module, "get_connection", _fail_if_called)
    monkeypatch.setattr("unflincher.probe.run_probe", _fake_run_probe)
    monkeypatch.setattr("unflincher.llm.shutdown_client", _fake_shutdown)

    exit_code = cli_module.main(["probe"])

    assert exit_code == 0
