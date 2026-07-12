#!/usr/bin/env bash
# Nightly WAL-safe SQLite online backup. The artifact is written under a hidden .partial name,
# verified after compression, and atomically renamed only after every check passes.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="${UNFLINCHER_BACKUP_DIR:-$HOME/backups/unflincher}"
CONTAINER="${UNFLINCHER_CONTAINER:-unflincher}"
DB="${UNFLINCHER_DB_PATH:-/data/unflincher.db}"
RETENTION_DAYS="${UNFLINCHER_BACKUP_RETENTION_DAYS:-30}"
VERIFY_SCRIPT="${UNFLINCHER_BACKUP_VERIFY_SCRIPT:-$SCRIPT_DIR/verify-unflincher-backup.py}"

umask 077
mkdir -p "$BACKUP_DIR"
chmod 0700 "$BACKUP_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/unflincher-${STAMP}.db.gz"
PARTIAL="$BACKUP_DIR/.unflincher-${STAMP}-$$.db.gz.partial"
CONTAINER_TMP="/tmp/unflincher-${STAMP}-$$.db"

cleanup() {
  rm -f "$PARTIAL"
  podman exec "$CONTAINER" rm -f "$CONTAINER_TMP" >/dev/null 2>&1 || true
}
trap cleanup EXIT

COUNT_BEFORE="$(
  podman exec "$CONTAINER" sqlite3 "$DB" "SELECT COUNT(*) FROM diary_entry;"
)"
if [[ ! "$COUNT_BEFORE" =~ ^[0-9]+$ ]]; then
  echo "unflincher-backup: invalid live entry count before backup: $COUNT_BEFORE" >&2
  exit 1
fi

podman exec "$CONTAINER" sqlite3 "$DB" ".backup '$CONTAINER_TMP'"
podman exec "$CONTAINER" gzip -c "$CONTAINER_TMP" > "$PARTIAL"

BACKUP_COUNT="$(
  python3 "$VERIFY_SCRIPT" "$PARTIAL"
)"
COUNT_AFTER="$(
  podman exec "$CONTAINER" sqlite3 "$DB" "SELECT COUNT(*) FROM diary_entry;"
)"
if [[ ! "$COUNT_AFTER" =~ ^[0-9]+$ ]]; then
  echo "unflincher-backup: invalid live entry count after backup: $COUNT_AFTER" >&2
  exit 1
fi

if (( COUNT_BEFORE <= COUNT_AFTER )); then
  LOWER_COUNT="$COUNT_BEFORE"
  UPPER_COUNT="$COUNT_AFTER"
else
  LOWER_COUNT="$COUNT_AFTER"
  UPPER_COUNT="$COUNT_BEFORE"
fi
if (( BACKUP_COUNT < LOWER_COUNT || BACKUP_COUNT > UPPER_COUNT )); then
  echo "unflincher-backup: backup entry count $BACKUP_COUNT is outside live range ${LOWER_COUNT}..${UPPER_COUNT}" >&2
  exit 1
fi

mv "$PARTIAL" "$OUT"
chmod 0600 "$OUT"

podman exec "$CONTAINER" rm -f "$CONTAINER_TMP" >/dev/null 2>&1 || true
trap - EXIT

find "$BACKUP_DIR" -maxdepth 1 -name 'unflincher-*.db.gz' -type f \
  -mtime "+${RETENTION_DAYS}" -delete
echo "unflincher-backup: wrote $OUT ($(du -h "$OUT" | cut -f1)); verified entries=$BACKUP_COUNT (live before=$COUNT_BEFORE after=$COUNT_AFTER); pruned > ${RETENTION_DAYS}d"
