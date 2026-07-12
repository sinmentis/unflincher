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
