#!/usr/bin/env bash
# deploy/scripts/diary-backup.sh
# Nightly WAL-safe SQLite online backup, streamed out of the container to a dated 0600
# gzip; prunes anything older than the retention window. Never `cp`s the live DB file —
# that can miss un-checkpointed WAL pages (technical design §6.7/§7.6 point 4).
set -euo pipefail
BACKUP_DIR="${DIARY_BACKUP_DIR:-$HOME/backups/diary}"
CONTAINER="${DIARY_CONTAINER:-diary}"
DB="${DIARY_DB_PATH:-/data/diary.db}"
RETENTION_DAYS="${DIARY_BACKUP_RETENTION_DAYS:-30}"

umask 077
mkdir -p "$BACKUP_DIR"
chmod 0700 "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/diary-${STAMP}.db.gz"

podman exec "$CONTAINER" sh -c \
  "sqlite3 '$DB' \".backup '/tmp/diary-${STAMP}.db'\" && gzip -c '/tmp/diary-${STAMP}.db' && rm -f '/tmp/diary-${STAMP}.db'" \
  > "$OUT"
chmod 0600 "$OUT"
find "$BACKUP_DIR" -maxdepth 1 -name 'diary-*.db.gz' -type f -mtime "+${RETENTION_DAYS}" -delete
echo "diary-backup: wrote $OUT ($(du -h "$OUT" | cut -f1)); pruned > ${RETENTION_DAYS}d"
