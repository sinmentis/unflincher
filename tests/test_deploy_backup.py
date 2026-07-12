import gzip
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


def _write_backup_archive(tmp_path: Path, entry_count: int = 2) -> Path:
    db_path = tmp_path / "source.db"
    archive_path = tmp_path / "source.db.gz"
    conn = sqlite3.connect(db_path)
    tables = (
        "diary_entry",
        "persona_prompt",
        "entry_commentary",
        "aggregate_report",
        "chat_message",
        "chat_session",
        "regen_job",
        "regen_job_item",
    )
    for table in tables:
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        conn.executemany(
            f"INSERT INTO {table} (id) VALUES (?)",
            [(index + 1,) for index in range(entry_count)],
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
    tmp_path: Path, archive: Path, restored_count: int, curl_exit: int = 0
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


def test_restore_drill_has_valid_bash_syntax():
    result = subprocess.run(
        ["bash", "-n", str(RESTORE_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
