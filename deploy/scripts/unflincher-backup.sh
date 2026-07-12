#!/usr/bin/env bash
# deploy/scripts/unflincher-backup.sh
# Nightly WAL-safe SQLite online backup, streamed out of the container to a dated 0600
# gzip; prunes anything older than the retention window. Never `cp`s the live DB file --
# that can miss un-checkpointed WAL pages (the app runs SQLite in WAL mode).
set -euo pipefail
BACKUP_DIR="${UNFLINCHER_BACKUP_DIR:-$HOME/backups/unflincher}"
CONTAINER="${UNFLINCHER_CONTAINER:-unflincher}"
DB="${UNFLINCHER_DB_PATH:-/data/unflincher.db}"
RETENTION_DAYS="${UNFLINCHER_BACKUP_RETENTION_DAYS:-30}"

umask 077
mkdir -p "$BACKUP_DIR"
chmod 0700 "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/unflincher-${STAMP}.db.gz"

podman exec "$CONTAINER" sh -c \
  "sqlite3 '$DB' \".backup '/tmp/unflincher-${STAMP}.db'\" && gzip -c '/tmp/unflincher-${STAMP}.db' && rm -f '/tmp/unflincher-${STAMP}.db'" \
  > "$OUT"
chmod 0600 "$OUT"
find "$BACKUP_DIR" -maxdepth 1 -name 'unflincher-*.db.gz' -type f -mtime "+${RETENTION_DAYS}" -delete
echo "unflincher-backup: wrote $OUT ($(du -h "$OUT" | cut -f1)); pruned > ${RETENTION_DAYS}d"
