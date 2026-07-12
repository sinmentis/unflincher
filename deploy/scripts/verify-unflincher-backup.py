#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path


class BackupVerificationError(RuntimeError):
    pass


COUNT_TABLES = (
    "diary_entry",
    "persona_prompt",
    "entry_commentary",
    "aggregate_report",
    "chat_message",
    "chat_session",
    "regen_job",
    "regen_job_item",
)


def verify_backup(
    archive: Path, expected_entry_count: int | None = None
) -> dict[str, int]:
    if not archive.is_file():
        raise BackupVerificationError(f"backup file does not exist: {archive}")

    with tempfile.NamedTemporaryFile(suffix=".db") as restored:
        try:
            with gzip.open(archive, "rb") as source:
                shutil.copyfileobj(source, restored)
            restored.flush()
        except (gzip.BadGzipFile, EOFError, OSError) as exc:
            raise BackupVerificationError(f"cannot decompress backup: {exc}") from exc

        try:
            conn = sqlite3.connect(f"file:{restored.name}?mode=ro", uri=True)
            try:
                integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
                if integrity != ["ok"]:
                    raise BackupVerificationError(
                        f"SQLite integrity_check failed: {'; '.join(integrity)}"
                    )
                counts = {
                    table: conn.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                    for table in COUNT_TABLES
                }
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            raise BackupVerificationError(f"invalid SQLite backup: {exc}") from exc

    entry_count = counts["diary_entry"]
    if expected_entry_count is not None and entry_count != expected_entry_count:
        raise BackupVerificationError(
            f"expected {expected_entry_count} diary entries, found {entry_count}"
        )
    return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a gzip-compressed Unflincher SQLite backup."
    )
    parser.add_argument("archive", type=Path)
    parser.add_argument("--expected-entry-count", type=int)
    parser.add_argument(
        "--manifest",
        action="store_true",
        help="Print table=count lines instead of only the diary-entry count.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.expected_entry_count is not None and args.expected_entry_count < 0:
        print(
            "backup verification failed: expected entry count cannot be negative",
            file=sys.stderr,
        )
        return 1
    try:
        counts = verify_backup(args.archive, args.expected_entry_count)
    except BackupVerificationError as exc:
        print(f"backup verification failed: {exc}", file=sys.stderr)
        return 1
    if args.manifest:
        for table in COUNT_TABLES:
            print(f"{table}={counts[table]}")
    else:
        print(counts["diary_entry"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
