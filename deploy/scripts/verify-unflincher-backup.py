#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
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

# The exact fields an active-prompt preservation manifest must include (see the plan's
# Persistence and migration section, item 12) -- id, version_no, a UTF-8 SHA-256 of body_text
# (never the body text itself), model, is_active, created_at, and preset_key. Order is fixed so
# both the CLI's key=value output and the restore drill's field-by-field bash comparison stay in
# lockstep with this one list.
ACTIVE_PROMPT_MANIFEST_FIELDS = (
    "id", "version_no", "body_sha256", "model", "is_active", "created_at", "preset_key",
)

# Printed for a NULL preset_key so the key=value output is unambiguous (an actual empty string
# never occurs in any of these fields) -- "Custom" prompts are the common case, not an edge case.
_NULL_SENTINEL = "NULL"


def active_prompt_manifest(conn: sqlite3.Connection) -> dict[str, object] | None:
    """Returns the active persona_prompt row's identity manifest WITHOUT its private body text:
    id, version_no, body_sha256 (sha256(body_text.encode('utf-8')), raw bytes, no synthetic
    newline), model, is_active, created_at, preset_key.

    Returns None only when persona_prompt is absent, or it has no active row. Raises
    BackupVerificationError for anything else: a missing identity column
    (id/version_no/body_text/is_active/created_at), a non-TEXT body_text, or more than one active
    row (a violation of the app's own is_active uniqueness invariant). model/preset_key are the
    only historically optional columns -- read as None if absent.

    Reads by explicit column list and positional index rather than name-based row access, so it
    never needs to set (and never leaves mutated) the caller's conn.row_factory."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "persona_prompt" not in tables:
        return None

    columns = {row[1] for row in conn.execute("PRAGMA table_info(persona_prompt)")}
    required_columns = ("id", "version_no", "body_text", "is_active", "created_at")
    missing = set(required_columns) - columns
    if missing:
        raise BackupVerificationError(
            f"persona_prompt is missing required column(s): {sorted(missing)}"
        )

    optional_columns = [name for name in ("model", "preset_key") if name in columns]
    select_columns = [*required_columns, *optional_columns]
    rows = conn.execute(
        f"SELECT {', '.join(select_columns)} FROM persona_prompt WHERE is_active = 1"
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise BackupVerificationError(
            f"persona_prompt has {len(rows)} active rows; expected at most one"
        )

    values = dict(zip(select_columns, rows[0]))
    body_text = values["body_text"]
    if not isinstance(body_text, str):
        raise BackupVerificationError(
            f"persona_prompt.body_text is not TEXT (got {type(body_text).__name__})"
        )
    return {
        "id": values["id"],
        "version_no": values["version_no"],
        "body_sha256": hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
        "model": values.get("model"),
        "is_active": values["is_active"],
        "created_at": values["created_at"],
        "preset_key": values.get("preset_key"),
    }


def format_active_prompt_manifest(manifest: dict[str, object] | None) -> list[str]:
    """Renders an active_prompt_manifest() result as deterministic key=value lines (empty list if
    there is no active prompt at all), substituting _NULL_SENTINEL for a None preset_key/model so
    the output is unambiguous plain text a bash restore drill can diff line-by-line."""
    if manifest is None:
        return []
    return [
        f"{field}={_NULL_SENTINEL if manifest[field] is None else manifest[field]}"
        for field in ACTIVE_PROMPT_MANIFEST_FIELDS
    ]


def read_active_prompt_manifest_from_archive(archive: Path) -> dict[str, object] | None:
    """Decompress the given gzip archive to a throwaway temp file and read its active prompt's
    manifest (see active_prompt_manifest). Deliberately independent of verify_backup() -- a
    caller can request just this manifest without paying for the full row-count verification
    pass, and verify_backup()'s own already-tested return shape never has to change."""
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
                return active_prompt_manifest(conn)
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            raise BackupVerificationError(f"invalid SQLite backup: {exc}") from exc


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
    parser.add_argument(
        "--active-prompt-manifest",
        action="store_true",
        help=(
            "Print the active persona_prompt's identity manifest (id, version_no, a UTF-8 "
            "SHA-256 of body_text, model, is_active, created_at, preset_key) as key=value "
            "lines -- never the private body text itself. Independent of --manifest/"
            "--expected-entry-count; skips the row-count/integrity verification pass."
        ),
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

    if args.active_prompt_manifest:
        try:
            manifest = read_active_prompt_manifest_from_archive(args.archive)
        except BackupVerificationError as exc:
            print(f"backup verification failed: {exc}", file=sys.stderr)
            return 1
        for line in format_active_prompt_manifest(manifest):
            print(line)
        return 0

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
