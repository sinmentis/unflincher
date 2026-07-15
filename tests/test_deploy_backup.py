import gzip
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERIFY_SCRIPT = ROOT / "deploy/scripts/verify-unflincher-backup.py"
BACKUP_SCRIPT = ROOT / "deploy/scripts/unflincher-backup.sh"
RESTORE_SCRIPT = ROOT / "deploy/scripts/unflincher-restore-drill.sh"
DEPLOY_SCRIPT = ROOT / "deploy/scripts/deploy-unflincher.sh"


def _write_backup_archive(tmp_path: Path, entry_count: int = 2) -> Path:
    db_path = tmp_path / "source.db"
    archive_path = tmp_path / "source.db.gz"
    conn = sqlite3.connect(db_path)
    id_only_tables = (
        "diary_entry", "entry_commentary", "aggregate_report",
        "chat_message", "chat_session", "regen_job", "regen_job_item",
    )
    for table in id_only_tables:
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        conn.executemany(
            f"INSERT INTO {table} (id) VALUES (?)",
            [(index + 1,) for index in range(entry_count)],
        )
    # A VALID persona_prompt schema (not just an `id` column) with every row INACTIVE -- proves
    # active_prompt_manifest() correctly returns None ("no active row") rather than raising for a
    # malformed schema (see item 6). Row count still matches every other table.
    conn.execute(
        "CREATE TABLE persona_prompt (id INTEGER PRIMARY KEY, version_no INTEGER, "
        "body_text TEXT, model TEXT, is_active INTEGER, preset_key TEXT, created_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO persona_prompt (id, version_no, body_text, model, is_active, preset_key, created_at) "
        "VALUES (?, ?, 'inactive', 'gpt-5.4', 0, NULL, '2026-01-01T00:00:00+00:00')",
        [(index + 1, index + 1) for index in range(entry_count)],
    )
    conn.commit()
    conn.close()
    with db_path.open("rb") as source, gzip.open(archive_path, "wb") as target:
        shutil.copyfileobj(source, target)
    return archive_path


def _write_backup_archive_with_real_active_prompt(
    tmp_path: Path, entry_count: int = 2, *, body_text: str, model: str, preset_key: str | None,
    version_no: int = 1, prompt_id: int = 1, created_at: str = "2026-01-01T00:00:00+00:00",
) -> Path:
    """Same COUNT_TABLES shape as _write_backup_archive, except persona_prompt gets a REAL row
    (id/version_no/body_text/model/is_active/preset_key/created_at) so the restore drill's
    active-prompt manifest comparison has something real to compare, end to end."""
    db_path = tmp_path / "source-with-prompt.db"
    archive_path = tmp_path / "source-with-prompt.db.gz"
    conn = sqlite3.connect(db_path)
    id_only_tables = (
        "diary_entry", "entry_commentary", "aggregate_report",
        "chat_message", "chat_session", "regen_job", "regen_job_item",
    )
    for table in id_only_tables:
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        conn.executemany(
            f"INSERT INTO {table} (id) VALUES (?)",
            [(index + 1,) for index in range(entry_count)],
        )
    conn.execute(
        "CREATE TABLE persona_prompt (id INTEGER PRIMARY KEY, version_no INTEGER, "
        "body_text TEXT, model TEXT, is_active INTEGER, preset_key TEXT, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO persona_prompt VALUES (?, ?, ?, ?, 1, ?, ?)",
        (prompt_id, version_no, body_text, model, preset_key, created_at),
    )
    # Pad with inactive filler rows so persona_prompt's row COUNT also matches entry_count, like
    # every other table -- only the one row above (is_active=1) participates in the manifest
    # comparison the restore drill performs.
    for filler_id in range(entry_count - 1):
        conn.execute(
            "INSERT INTO persona_prompt (id, version_no, body_text, model, is_active, "
            "preset_key, created_at) VALUES (?, ?, 'superseded', 'gpt-5.4', 0, NULL, ?)",
            (prompt_id + filler_id + 1000, filler_id + 1, created_at),
        )
    conn.commit()
    conn.close()
    with db_path.open("rb") as source, gzip.open(archive_path, "wb") as target:
        shutil.copyfileobj(source, target)
    return archive_path


def _run_verifier(archive: Path, expected: int | None = None) -> subprocess.CompletedProcess:
    command = [sys.executable, str(VERIFY_SCRIPT), str(archive)]
    if expected is not None:
        command.extend(["--expected-entry-count", str(expected)])
    return subprocess.run(command, capture_output=True, text=True)


def test_backup_verifier_prints_verified_entry_count(tmp_path):
    archive = _write_backup_archive(tmp_path, entry_count=2)

    result = _run_verifier(archive, expected=2)

    assert result.returncode == 0
    assert result.stdout == "2\n"
    assert result.stderr == ""


def test_backup_verifier_prints_deterministic_manifest(tmp_path):
    archive = _write_backup_archive(tmp_path, entry_count=2)

    result = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), str(archive), "--manifest"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "diary_entry=2",
        "persona_prompt=2",
        "entry_commentary=2",
        "aggregate_report=2",
        "chat_message=2",
        "chat_session=2",
        "regen_job=2",
        "regen_job_item=2",
    ]
    assert result.stderr == ""


def _write_real_schema_archive_with_active_prompt(
    tmp_path: Path, *, body_text: str = "the owner's private instructions",
    model: str = "gpt-5.4", preset_key: str | None = "analyst",
    version_no: int = 1, prompt_id: int = 1, created_at: str = "2026-01-01T00:00:00+00:00",
) -> Path:
    """Unlike _write_backup_archive (whose tables only carry a bare `id` column, sufficient for
    the row-count manifest), this writes a REAL persona_prompt table shape so
    --active-prompt-manifest has real id/version_no/body_text/model/is_active/created_at/
    preset_key fields to read."""
    db_path = tmp_path / "with-prompt.db"
    archive_path = tmp_path / "with-prompt.db.gz"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE persona_prompt (id INTEGER PRIMARY KEY, version_no INTEGER, "
        "body_text TEXT, model TEXT, is_active INTEGER, preset_key TEXT, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO persona_prompt VALUES (?, ?, ?, ?, 1, ?, ?)",
        (prompt_id, version_no, body_text, model, preset_key, created_at),
    )
    conn.commit()
    conn.close()
    with db_path.open("rb") as source, gzip.open(archive_path, "wb") as target:
        shutil.copyfileobj(source, target)
    return archive_path


def test_backup_verifier_prints_active_prompt_manifest_without_the_body_text(tmp_path):
    body_text = "the owner's private instructions, never printed anywhere"
    archive = _write_real_schema_archive_with_active_prompt(tmp_path, body_text=body_text)

    result = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), str(archive), "--active-prompt-manifest"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    # body_sha256 hashes the RAW UTF-8 bytes of body_text, with no synthetic trailing newline.
    expected_sha = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
    assert result.stdout.splitlines() == [
        "id=1",
        "version_no=1",
        f"body_sha256={expected_sha}",
        "model=gpt-5.4",
        "is_active=1",
        "created_at=2026-01-01T00:00:00+00:00",
        "preset_key=analyst",
    ]
    # The private body text itself must never appear anywhere in the output.
    assert body_text not in result.stdout
    assert body_text not in result.stderr


def test_backup_verifier_active_prompt_manifest_hashes_multiline_non_ascii_body_text(tmp_path):
    """UTF-8 correctness: a multiline body containing non-ASCII characters (CJK + an emoji, via
    ASCII source escapes so the test file itself stays ASCII) must hash identically to Python's
    own sha256(body_text.encode('utf-8')) -- no normalization, no line-ending rewriting, no
    synthetic trailing newline."""
    body_text = (
        "\u7b2c\u4e00\u884c\u6307\u4ee4\n"
        "\u7b2c\u4e8c\u884c,\u542b\u6709 emoji \U0001f30a \u548c\u6807\u70b9\u3002\n"
        "\u6700\u540e\u4e00\u884c\u3002"
    )
    archive = _write_real_schema_archive_with_active_prompt(tmp_path, body_text=body_text)

    result = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), str(archive), "--active-prompt-manifest"],
        capture_output=True, text=True,
    )

    assert result.returncode == 0
    expected_sha = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
    assert f"body_sha256={expected_sha}" in result.stdout.splitlines()
    assert body_text not in result.stdout


def test_backup_verifier_active_prompt_manifest_uses_null_sentinel_for_custom(tmp_path):
    archive = _write_real_schema_archive_with_active_prompt(tmp_path, preset_key=None)

    result = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), str(archive), "--active-prompt-manifest"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "preset_key=NULL" in result.stdout.splitlines()


def test_backup_verifier_active_prompt_manifest_is_deterministic_and_reproducible(tmp_path):
    archive = _write_real_schema_archive_with_active_prompt(tmp_path)

    first = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), str(archive), "--active-prompt-manifest"],
        capture_output=True, text=True,
    )
    second = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), str(archive), "--active-prompt-manifest"],
        capture_output=True, text=True,
    )

    assert first.stdout == second.stdout
    assert first.returncode == second.returncode == 0


def test_backup_verifier_active_prompt_manifest_empty_when_no_active_prompt(tmp_path):
    db_path = tmp_path / "no-active.db"
    archive_path = tmp_path / "no-active.db.gz"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE persona_prompt (id INTEGER PRIMARY KEY, version_no INTEGER, "
        "body_text TEXT, model TEXT, is_active INTEGER, preset_key TEXT, created_at TEXT)"
    )
    conn.commit()
    conn.close()
    with db_path.open("rb") as source, gzip.open(archive_path, "wb") as target:
        shutil.copyfileobj(source, target)

    result = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), str(archive_path), "--active-prompt-manifest"],
        capture_output=True, text=True,
    )

    assert result.returncode == 0
    assert result.stdout == ""


def test_backup_verifier_active_prompt_manifest_empty_when_table_absent(tmp_path):
    db_path = tmp_path / "no-table.db"
    archive_path = tmp_path / "no-table.db.gz"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE diary_entry (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    with db_path.open("rb") as source, gzip.open(archive_path, "wb") as target:
        shutil.copyfileobj(source, target)

    result = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), str(archive_path), "--active-prompt-manifest"],
        capture_output=True, text=True,
    )

    assert result.returncode == 0
    assert result.stdout == ""


def test_backup_verifier_active_prompt_manifest_rejects_malformed_schema(tmp_path):
    """A persona_prompt missing one of its original identity columns must FAIL, not silently
    return None -- item 6 explicitly forbids treating a malformed schema the same as "no active
    row"."""
    db_path = tmp_path / "malformed.db"
    archive_path = tmp_path / "malformed.db.gz"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE persona_prompt (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO persona_prompt (id) VALUES (1)")
    conn.commit()
    conn.close()
    with db_path.open("rb") as source, gzip.open(archive_path, "wb") as target:
        shutil.copyfileobj(source, target)

    result = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), str(archive_path), "--active-prompt-manifest"],
        capture_output=True, text=True,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "missing required column" in result.stderr


def test_backup_verifier_active_prompt_manifest_rejects_multiple_active_rows(tmp_path):
    """More than one is_active=1 row violates the app's own uniqueness invariant -- the manifest
    tool must fail loudly rather than silently pick one via LIMIT/fetchone()."""
    db_path = tmp_path / "multi-active.db"
    archive_path = tmp_path / "multi-active.db.gz"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE persona_prompt (id INTEGER PRIMARY KEY, version_no INTEGER, "
        "body_text TEXT, model TEXT, is_active INTEGER, preset_key TEXT, created_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO persona_prompt (id, version_no, body_text, model, is_active, preset_key, created_at) "
        "VALUES (?, ?, 'x', 'gpt-5.4', 1, NULL, '2026-01-01T00:00:00+00:00')",
        [(1, 1), (2, 2)],
    )
    conn.commit()
    conn.close()
    with db_path.open("rb") as source, gzip.open(archive_path, "wb") as target:
        shutil.copyfileobj(source, target)

    result = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), str(archive_path), "--active-prompt-manifest"],
        capture_output=True, text=True,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "2 active rows" in result.stderr


def test_backup_verifier_active_prompt_manifest_rejects_non_text_body(tmp_path):
    """A malformed body_text (stored as a BLOB -- SQLite's TEXT-affinity coercion does not apply
    to blobs, unlike numeric literals) must raise a stable BackupVerificationError, never leak an
    AttributeError from calling .encode() on a non-str."""
    db_path = tmp_path / "non-text-body.db"
    archive_path = tmp_path / "non-text-body.db.gz"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE persona_prompt (id INTEGER PRIMARY KEY, version_no INTEGER, "
        "body_text TEXT, model TEXT, is_active INTEGER, preset_key TEXT, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO persona_prompt (id, version_no, body_text, model, is_active, preset_key, created_at) "
        "VALUES (1, 1, ?, 'gpt-5.4', 1, NULL, '2026-01-01T00:00:00+00:00')",
        (b"\x00\x01raw bytes, not text",),
    )
    conn.commit()
    conn.close()
    with db_path.open("rb") as source, gzip.open(archive_path, "wb") as target:
        shutil.copyfileobj(source, target)

    result = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), str(archive_path), "--active-prompt-manifest"],
        capture_output=True, text=True,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "not TEXT" in result.stderr
    assert "AttributeError" not in result.stderr
    assert "Traceback" not in result.stderr


def test_active_prompt_manifest_does_not_mutate_the_callers_row_factory(tmp_path):
    """active_prompt_manifest must never leave conn.row_factory changed -- it reads by explicit
    column list and positional index, not name-based row access."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("verify_unflincher_backup", VERIFY_SCRIPT)
    verify_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verify_module)

    db_path = tmp_path / "row-factory.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE persona_prompt (id INTEGER PRIMARY KEY, version_no INTEGER, "
        "body_text TEXT, model TEXT, is_active INTEGER, preset_key TEXT, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO persona_prompt (id, version_no, body_text, model, is_active, preset_key, created_at) "
        "VALUES (1, 1, 'x', 'gpt-5.4', 1, NULL, '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()

    sentinel_factory = sqlite3.Row
    conn.row_factory = sentinel_factory
    manifest = verify_module.active_prompt_manifest(conn)

    assert manifest is not None
    assert conn.row_factory is sentinel_factory  # unchanged
    conn.close()


def test_backup_verifier_active_prompt_manifest_rejects_invalid_gzip(tmp_path):
    archive = tmp_path / "broken.db.gz"
    archive.write_bytes(b"not a gzip stream")

    result = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), str(archive), "--active-prompt-manifest"],
        capture_output=True, text=True,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "cannot decompress backup" in result.stderr


def test_backup_verifier_rejects_entry_count_mismatch(tmp_path):
    archive = _write_backup_archive(tmp_path, entry_count=2)

    result = _run_verifier(archive, expected=3)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "expected 3 diary entries, found 2" in result.stderr


def test_backup_verifier_rejects_invalid_gzip(tmp_path):
    archive = tmp_path / "broken.db.gz"
    archive.write_bytes(b"not a gzip stream")

    result = _run_verifier(archive)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "cannot decompress backup" in result.stderr


def test_backup_verifier_rejects_corrupt_sqlite(tmp_path):
    archive = tmp_path / "corrupt.db.gz"
    with gzip.open(archive, "wb") as target:
        target.write(b"not a SQLite database")

    result = _run_verifier(archive)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "invalid SQLite backup" in result.stderr


def test_backup_verifier_rejects_a_different_sqlite_schema(tmp_path):
    db_path = tmp_path / "wrong.db"
    archive = tmp_path / "wrong.db.gz"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE diary_entry (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    with db_path.open("rb") as source, gzip.open(archive, "wb") as target:
        shutil.copyfileobj(source, target)

    result = _run_verifier(archive)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "no such table: persona_prompt" in result.stderr


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _write_fake_deploy_commands(fake_bin: Path) -> Path:
    curl_count_path = fake_bin / "curl.count"
    _write_executable(
        fake_bin / "podman",
        """#!/usr/bin/env bash
set -euo pipefail
exit 0
""",
    )
    _write_executable(
        fake_bin / "systemctl",
        """#!/usr/bin/env bash
set -euo pipefail
exit 0
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
set -euo pipefail
count=0
if [[ -f "$FAKE_CURL_COUNT" ]]; then
  read -r count < "$FAKE_CURL_COUNT"
fi
count=$((count + 1))
printf '%s\n' "$count" > "$FAKE_CURL_COUNT"
if (( count < FAKE_CURL_SUCCEED_ON )); then
  printf 'FAKE_CURL_STARTUP_PROBE_RECV_FAILURE\n' >&2
  exit 56
fi
printf '{"status":"ok"}\n'
""",
    )
    _write_executable(
        fake_bin / "sleep",
        """#!/usr/bin/env bash
exit 0
""",
    )
    return curl_count_path


def _run_deploy_script(
    tmp_path: Path, curl_succeed_on: int
) -> tuple[subprocess.CompletedProcess, int]:
    fake_bin = tmp_path / "deploy-fake-bin"
    fake_bin.mkdir()
    curl_count_path = _write_fake_deploy_commands(fake_bin)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "FAKE_CURL_COUNT": str(curl_count_path),
            "FAKE_CURL_SUCCEED_ON": str(curl_succeed_on),
        }
    )
    result = subprocess.run(
        ["bash", str(DEPLOY_SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    return result, int(curl_count_path.read_text())


def test_deploy_script_retries_transient_startup_failures(tmp_path):
    result, curl_count = _run_deploy_script(tmp_path, curl_succeed_on=3)

    assert result.returncode == 0
    assert curl_count == 3
    assert "unflincher.service deployed and healthy." in result.stdout
    assert "FAKE_CURL_STARTUP_PROBE_RECV_FAILURE" not in result.stderr


def test_deploy_script_reports_terminal_health_failure(tmp_path):
    result, curl_count = _run_deploy_script(tmp_path, curl_succeed_on=61)

    assert result.returncode == 1
    assert curl_count == 60
    assert "deployment failed: unflincher.service did not become healthy" in result.stderr
    assert "FAKE_CURL_STARTUP_PROBE_RECV_FAILURE" not in result.stderr


def _write_fake_backup_podman(fake_bin: Path) -> Path:
    log_path = fake_bin / "podman.log"
    _write_executable(
        fake_bin / "podman",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_PODMAN_LOG"
if [[ "$*" == *"SELECT COUNT(*) FROM "* ]]; then
  printf '%s\n' "$FAKE_ENTRY_COUNT"
elif [[ "$*" == *"gzip -c"* ]]; then
  cat "$FAKE_BACKUP_ARCHIVE"
fi
""",
    )
    return log_path


def _run_backup_script(
    tmp_path: Path, archive: Path, live_count: int
) -> subprocess.CompletedProcess:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    log_path = _write_fake_backup_podman(fake_bin)
    backup_dir = tmp_path / "backups"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "FAKE_BACKUP_ARCHIVE": str(archive),
            "FAKE_ENTRY_COUNT": str(live_count),
            "FAKE_PODMAN_LOG": str(log_path),
            "UNFLINCHER_BACKUP_DIR": str(backup_dir),
            "UNFLINCHER_BACKUP_VERIFY_SCRIPT": str(VERIFY_SCRIPT),
        }
    )
    return subprocess.run(
        ["bash", str(BACKUP_SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_backup_script_publishes_only_a_verified_archive(tmp_path):
    archive = _write_backup_archive(tmp_path, entry_count=2)

    result = _run_backup_script(tmp_path, archive, live_count=2)

    backup_dir = tmp_path / "backups"
    files = list(backup_dir.iterdir())
    assert result.returncode == 0
    assert len(files) == 1
    assert files[0].name.startswith("unflincher-")
    assert files[0].name.endswith(".db.gz")
    assert files[0].stat().st_mode & 0o777 == 0o600
    assert "verified entries=2" in result.stdout


def test_backup_script_removes_partial_archive_when_verification_fails(tmp_path):
    archive = _write_backup_archive(tmp_path, entry_count=2)

    result = _run_backup_script(tmp_path, archive, live_count=3)

    backup_dir = tmp_path / "backups"
    assert result.returncode != 0
    assert list(backup_dir.iterdir()) == []
    assert "backup entry count 2 is outside live range 3..3" in result.stderr


def test_backup_script_has_valid_bash_syntax():
    result = subprocess.run(
        ["bash", "-n", str(BACKUP_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def _write_fake_restore_commands(fake_bin: Path) -> Path:
    log_path = fake_bin / "podman.log"
    _write_executable(
        fake_bin / "podman",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_PODMAN_LOG"
if [[ "$*" == *"SELECT COUNT(*) FROM "* ]]; then
  printf '%s\n' "$FAKE_RESTORED_COUNT"
elif [[ "$*" == *"SELECT id FROM diary_entry ORDER BY id LIMIT 1;"* ]]; then
  printf '1\n'
elif [[ "$*" == *"SELECT id FROM persona_prompt WHERE is_active=1;"* ]]; then
  printf '%s\n' "${FAKE_PROMPT_ID:-}"
elif [[ "$*" == *"SELECT version_no FROM persona_prompt WHERE is_active=1;"* ]]; then
  printf '%s\n' "${FAKE_PROMPT_VERSION_NO:-}"
elif [[ "$*" == *"SELECT model FROM persona_prompt WHERE is_active=1;"* ]]; then
  printf '%s\n' "${FAKE_PROMPT_MODEL:-}"
elif [[ "$*" == *"SELECT is_active FROM persona_prompt WHERE is_active=1;"* ]]; then
  printf '%s\n' "${FAKE_PROMPT_IS_ACTIVE:-}"
elif [[ "$*" == *"SELECT created_at FROM persona_prompt WHERE is_active=1;"* ]]; then
  printf '%s\n' "${FAKE_PROMPT_CREATED_AT:-}"
elif [[ "$*" == *"SELECT COALESCE(preset_key,'NULL') FROM persona_prompt WHERE is_active=1;"* ]]; then
  printf '%s\n' "${FAKE_PROMPT_PRESET_KEY:-}"
elif [[ "$*" == *"python3 -c"* ]]; then
  printf '%s\n' "${FAKE_PROMPT_BODY_SHA256:-}"
elif [[ "${1:-}" == "run" && "$*" == *" -d "* ]]; then
  printf 'fake-container-id\n'
fi
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
if [[ "${FAKE_CURL_EXIT:-0}" != "0" ]]; then
  printf 'FAKE_CURL_STARTUP_PROBE_RECV_FAILURE\\n' >&2
fi
exit "${FAKE_CURL_EXIT:-0}"
""",
    )
    _write_executable(
        fake_bin / "sleep",
        """#!/usr/bin/env bash
exit 0
""",
    )
    return log_path


def _run_restore_drill(
    tmp_path: Path, archive: Path, restored_count: int, curl_exit: int = 0,
    prompt_fields: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess, str]:
    fake_bin = tmp_path / "restore-fake-bin"
    fake_bin.mkdir()
    log_path = _write_fake_restore_commands(fake_bin)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "FAKE_PODMAN_LOG": str(log_path),
            "FAKE_RESTORED_COUNT": str(restored_count),
            "FAKE_CURL_EXIT": str(curl_exit),
            "UNFLINCHER_BACKUP_VERIFY_SCRIPT": str(VERIFY_SCRIPT),
            "UNFLINCHER_RESTORE_PORT": "18097",
        }
    )
    if prompt_fields:
        body_sha256 = prompt_fields.get("body_sha256")
        if body_sha256 is None:
            body_sha256 = hashlib.sha256(prompt_fields["body_text"].encode("utf-8")).hexdigest()
        env.update(
            {
                "FAKE_PROMPT_ID": prompt_fields["id"],
                "FAKE_PROMPT_VERSION_NO": prompt_fields["version_no"],
                "FAKE_PROMPT_MODEL": prompt_fields["model"],
                "FAKE_PROMPT_IS_ACTIVE": prompt_fields["is_active"],
                "FAKE_PROMPT_CREATED_AT": prompt_fields["created_at"],
                "FAKE_PROMPT_PRESET_KEY": prompt_fields["preset_key"],
                # The restore drill computes the digest via `podman exec ... python3 -c ...`
                # (see item 5) -- the fake just prints this precomputed digest for that call.
                "FAKE_PROMPT_BODY_SHA256": body_sha256,
            }
        )
    result = subprocess.run(
        ["bash", str(RESTORE_SCRIPT), str(archive), "2"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    return result, log_path.read_text()


def test_restore_drill_checks_pages_and_cleans_disposable_resources(tmp_path):
    archive = _write_backup_archive(tmp_path, entry_count=2)

    result, podman_log = _run_restore_drill(tmp_path, archive, restored_count=2)

    assert result.returncode == 0
    assert "restore drill passed: entries=2" in result.stdout
    assert "volume create unflincher-restore-drill-" in podman_log
    assert "rm -f unflincher-restore-drill-" in podman_log
    assert "volume rm -f unflincher-restore-drill-" in podman_log
    assert "unflincher-data" not in podman_log


def test_restore_drill_health_failure_omits_container_logs_and_cleans_up(tmp_path):
    archive = _write_backup_archive(tmp_path, entry_count=2)

    result, podman_log = _run_restore_drill(
        tmp_path, archive, restored_count=2, curl_exit=1
    )

    assert result.returncode == 1
    assert (
        "restore drill failed: disposable app did not become healthy"
        in result.stderr
    )
    assert "logs unflincher-restore-drill-" not in podman_log
    assert "FAKE_CURL_STARTUP_PROBE_RECV_FAILURE" not in result.stderr
    assert "rm -f unflincher-restore-drill-" in podman_log
    assert "volume rm -f unflincher-restore-drill-" in podman_log
    assert "unflincher-data" not in podman_log


def test_restore_drill_fails_count_check_and_still_cleans_up(tmp_path):
    archive = _write_backup_archive(tmp_path, entry_count=2)

    result, podman_log = _run_restore_drill(tmp_path, archive, restored_count=3)

    assert result.returncode != 0
    assert (
        "restored table count mismatch: table=diary_entry archive=2 container=3"
        in result.stderr
    )
    assert "rm -f unflincher-restore-drill-" in podman_log
    assert "volume rm -f unflincher-restore-drill-" in podman_log


def test_restore_drill_passes_when_active_prompt_manifest_is_preserved(tmp_path):
    body_text = "the owner's private reflective instructions"
    archive = _write_backup_archive_with_real_active_prompt(
        tmp_path, entry_count=2, body_text=body_text, model="gpt-5.4", preset_key="analyst",
        version_no=3, prompt_id=7, created_at="2026-02-02T00:00:00+00:00",
    )

    result, podman_log = _run_restore_drill(
        tmp_path, archive, restored_count=2,
        prompt_fields={
            "id": "7", "version_no": "3", "model": "gpt-5.4", "is_active": "1",
            "created_at": "2026-02-02T00:00:00+00:00", "preset_key": "analyst",
            "body_text": body_text,
        },
    )

    assert result.returncode == 0
    assert "restore drill passed: entries=2" in result.stdout
    # The private body text itself must never appear in any drill output.
    assert body_text not in result.stdout
    assert body_text not in result.stderr
    assert body_text not in podman_log


def test_restore_drill_fails_when_active_prompt_preset_key_changes(tmp_path):
    body_text = "the owner's private reflective instructions"
    archive = _write_backup_archive_with_real_active_prompt(
        tmp_path, entry_count=2, body_text=body_text, model="gpt-5.4", preset_key="analyst",
    )

    result, podman_log = _run_restore_drill(
        tmp_path, archive, restored_count=2,
        prompt_fields={
            "id": "1", "version_no": "1", "model": "gpt-5.4", "is_active": "1",
            "created_at": "2026-01-01T00:00:00+00:00",
            "preset_key": "NULL",  # mismatch: archive says 'analyst'
            "body_text": body_text,
        },
    )

    assert result.returncode != 0
    assert (
        "active prompt manifest field mismatch: field=preset_key archive=analyst container=NULL"
        in result.stderr
    )
    assert "rm -f unflincher-restore-drill-" in podman_log
    assert "volume rm -f unflincher-restore-drill-" in podman_log


def test_restore_drill_fails_when_active_prompt_body_hash_changes(tmp_path):
    archive = _write_backup_archive_with_real_active_prompt(
        tmp_path, entry_count=2, body_text="original private instructions",
        model="gpt-5.4", preset_key=None,
    )

    result, _podman_log = _run_restore_drill(
        tmp_path, archive, restored_count=2,
        prompt_fields={
            "id": "1", "version_no": "1", "model": "gpt-5.4", "is_active": "1",
            "created_at": "2026-01-01T00:00:00+00:00", "preset_key": "NULL",
            "body_text": "a DIFFERENT body text",  # mismatch: different hash
        },
    )

    assert result.returncode != 0
    assert "active prompt manifest field mismatch: field=body_sha256" in result.stderr


def test_restore_drill_skips_active_prompt_check_when_archive_has_no_active_prompt(tmp_path):
    # A legacy backup predating persona_prompt/Analyst seeding: the archive side has no active
    # prompt at all, so the comparison must be skipped entirely (never fail for a genuinely
    # prompt-less legacy backup).
    archive = _write_backup_archive(tmp_path, entry_count=2)

    result, _podman_log = _run_restore_drill(
        tmp_path, archive, restored_count=2,
        prompt_fields={
            "id": "1", "version_no": "1", "model": "gpt-5.4", "is_active": "1",
            "created_at": "2026-01-01T00:00:00+00:00", "preset_key": "analyst",
            "body_text": "whatever the restored container happens to have",
        },
    )

    assert result.returncode == 0
    assert "restore drill passed: entries=2" in result.stdout


def test_restore_drill_fails_cleanly_when_the_restored_container_lost_its_active_prompt(tmp_path):
    """If the restored container has no active persona_prompt row at all (a real regression),
    the in-container Python must not traceback -- it emits an empty digest, and the surrounding
    field-by-field comparison fails cleanly with the normal mismatch message."""
    body_text = "the owner's private reflective instructions"
    archive = _write_backup_archive_with_real_active_prompt(
        tmp_path, entry_count=2, body_text=body_text, model="gpt-5.4", preset_key="analyst",
    )

    result, _podman_log = _run_restore_drill(
        tmp_path, archive, restored_count=2,
        prompt_fields={
            "id": "1", "version_no": "1", "model": "gpt-5.4", "is_active": "1",
            "created_at": "2026-01-01T00:00:00+00:00", "preset_key": "analyst",
            "body_sha256": "",  # simulates the real python3 -c printing '' for no active row
        },
    )

    assert result.returncode != 0
    assert "active prompt manifest field mismatch: field=body_sha256" in result.stderr
    assert "Traceback" not in result.stderr
    assert "NoneType" not in result.stderr


def test_restore_drill_has_valid_bash_syntax():
    result = subprocess.run(
        ["bash", "-n", str(RESTORE_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
