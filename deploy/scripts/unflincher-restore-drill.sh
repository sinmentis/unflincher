#!/usr/bin/env bash
# Restore one backup into disposable Podman resources and exercise the real application image.
# The production unflincher-data volume is never mounted.
set -euo pipefail

if [[ "$#" -lt 1 || "$#" -gt 2 ]]; then
  echo "usage: $0 BACKUP [EXPECTED_ENTRY_COUNT]" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP="$1"
EXPECTED_ENTRY_COUNT="${2:-}"
VERIFY_SCRIPT="${UNFLINCHER_BACKUP_VERIFY_SCRIPT:-$SCRIPT_DIR/verify-unflincher-backup.py}"
IMAGE="${UNFLINCHER_RESTORE_IMAGE:-localhost/unflincher:latest}"
PORT="${UNFLINCHER_RESTORE_PORT:-18096}"
RUN_ID="$$-$(date +%s)"
CONTAINER="unflincher-restore-drill-${RUN_ID}"
VOLUME="unflincher-restore-drill-${RUN_ID}"
TMP_DIR="$(mktemp -d)"
CONTAINER_STARTED=0
VOLUME_CREATED=0

cleanup() {
  if [[ "$CONTAINER_STARTED" == "1" ]]; then
    podman rm -f "$CONTAINER" >/dev/null 2>&1 || true
  fi
  if [[ "$VOLUME_CREATED" == "1" ]]; then
    podman volume rm -f "$VOLUME" >/dev/null 2>&1 || true
  fi
  rm -f "$TMP_DIR/unflincher.db"
  rmdir "$TMP_DIR" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

VERIFY_ARGS=("$BACKUP")
if [[ -n "$EXPECTED_ENTRY_COUNT" ]]; then
  VERIFY_ARGS+=("--expected-entry-count" "$EXPECTED_ENTRY_COUNT")
fi
MANIFEST="$(python3 "$VERIFY_SCRIPT" "${VERIFY_ARGS[@]}" --manifest)"
declare -A ARCHIVE_COUNTS=()
while IFS='=' read -r table count; do
  ARCHIVE_COUNTS["$table"]="$count"
done <<< "$MANIFEST"
ARCHIVE_COUNT="${ARCHIVE_COUNTS[diary_entry]}"

gunzip -c "$BACKUP" > "$TMP_DIR/unflincher.db"

podman volume create "$VOLUME" >/dev/null
VOLUME_CREATED=1
podman run --rm --pull=never --entrypoint sh \
  -v "${VOLUME}:/data:Z" \
  -v "${TMP_DIR}:/restore:ro,Z" \
  "$IMAGE" \
  -c "cp /restore/unflincher.db /data/unflincher.db"

podman run -d --rm --pull=never \
  --name "$CONTAINER" \
  -p "127.0.0.1:${PORT}:8000" \
  -v "${VOLUME}:/data:Z" \
  -e UNFLINCHER_DB=/data/unflincher.db \
  -e UNFLINCHER_REQUIRE_ACCESS_AUTH=false \
  "$IMAGE" >/dev/null
CONTAINER_STARTED=1

HEALTHY=0
for _ in {1..60}; do
  if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null; then
    HEALTHY=1
    break
  fi
  sleep 1
done
if [[ "$HEALTHY" != "1" ]]; then
  echo "restore drill failed: disposable app did not become healthy" >&2
  podman logs "$CONTAINER" >&2 || true
  exit 1
fi

COUNT_TABLES=(
  diary_entry
  persona_prompt
  entry_commentary
  aggregate_report
  chat_message
  chat_session
  regen_job
  regen_job_item
)
for table in "${COUNT_TABLES[@]}"; do
  RESTORED_COUNT="$(
    podman exec "$CONTAINER" sqlite3 /data/unflincher.db \
      "SELECT COUNT(*) FROM $table;"
  )"
  if [[ "$RESTORED_COUNT" != "${ARCHIVE_COUNTS[$table]}" ]]; then
    echo "restore drill failed: restored table count mismatch: table=$table archive=${ARCHIVE_COUNTS[$table]} container=$RESTORED_COUNT" >&2
    exit 1
  fi
done

FIRST_ENTRY_ID="$(
  podman exec "$CONTAINER" sqlite3 /data/unflincher.db \
    "SELECT id FROM diary_entry ORDER BY id LIMIT 1;"
)"
PATHS=("/" "/report" "/chat" "/workshop")
if [[ -n "$FIRST_ENTRY_ID" ]]; then
  PATHS+=("/entry/$FIRST_ENTRY_ID")
fi
for path in "${PATHS[@]}"; do
  curl -fsS -o /dev/null "http://127.0.0.1:${PORT}${path}"
done

echo "restore drill passed: entries=$ARCHIVE_COUNT; checked ${#PATHS[@]} pages; production volume untouched"
