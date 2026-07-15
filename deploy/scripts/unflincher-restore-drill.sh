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

# Active-prompt preservation manifest: id, version_no, a UTF-8 SHA-256 of body_text, model,
# is_active, created_at, preset_key -- captured from the archive now, compared against the
# restored container's active prompt once healthy, below. Never includes body_text itself.
BEFORE_PROMPT_MANIFEST="$(python3 "$VERIFY_SCRIPT" "$BACKUP" --active-prompt-manifest)"
declare -A BEFORE_PROMPT=()
while IFS='=' read -r field value; do
  [[ -n "$field" ]] && BEFORE_PROMPT["$field"]="$value"
done <<< "$BEFORE_PROMPT_MANIFEST"

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
  if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
    HEALTHY=1
    break
  fi
  sleep 1
done
if [[ "$HEALTHY" != "1" ]]; then
  echo "restore drill failed: disposable app did not become healthy" >&2
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

# Compare the active prompt's preservation manifest field-by-field, without ever printing the
# private body text -- only skipped when the ARCHIVE itself had no active prompt at all (e.g. a
# legacy backup predating persona_prompt or its Analyst seed), never when the container side is
# what's missing.
if [[ -n "${BEFORE_PROMPT[id]:-}" ]]; then
  AFTER_PROMPT_ID="$(podman exec "$CONTAINER" sqlite3 /data/unflincher.db "SELECT id FROM persona_prompt WHERE is_active=1;")"
  AFTER_PROMPT_VERSION_NO="$(podman exec "$CONTAINER" sqlite3 /data/unflincher.db "SELECT version_no FROM persona_prompt WHERE is_active=1;")"
  AFTER_PROMPT_MODEL="$(podman exec "$CONTAINER" sqlite3 /data/unflincher.db "SELECT model FROM persona_prompt WHERE is_active=1;")"
  AFTER_PROMPT_IS_ACTIVE="$(podman exec "$CONTAINER" sqlite3 /data/unflincher.db "SELECT is_active FROM persona_prompt WHERE is_active=1;")"
  AFTER_PROMPT_CREATED_AT="$(podman exec "$CONTAINER" sqlite3 /data/unflincher.db "SELECT created_at FROM persona_prompt WHERE is_active=1;")"
  AFTER_PROMPT_PRESET_KEY="$(podman exec "$CONTAINER" sqlite3 /data/unflincher.db "SELECT COALESCE(preset_key,'NULL') FROM persona_prompt WHERE is_active=1;")"
  # Compute the raw digest inside Python (the image is Python 3.12), matching
  # active_prompt_manifest()'s sha256(body_text.encode('utf-8')) exactly -- no synthetic
  # newline, and body_text itself is never printed, only the digest.
  AFTER_PROMPT_SHA="$(
    podman exec "$CONTAINER" python3 -c "
import hashlib, sqlite3
conn = sqlite3.connect('/data/unflincher.db')
row = conn.execute('SELECT body_text FROM persona_prompt WHERE is_active = 1').fetchone()
print(hashlib.sha256(row[0].encode('utf-8')).hexdigest() if row else '')
"
  )"

  declare -A AFTER_PROMPT=(
    [id]="$AFTER_PROMPT_ID"
    [version_no]="$AFTER_PROMPT_VERSION_NO"
    [body_sha256]="$AFTER_PROMPT_SHA"
    [model]="$AFTER_PROMPT_MODEL"
    [is_active]="$AFTER_PROMPT_IS_ACTIVE"
    [created_at]="$AFTER_PROMPT_CREATED_AT"
    [preset_key]="$AFTER_PROMPT_PRESET_KEY"
  )
  for field in id version_no body_sha256 model is_active created_at preset_key; do
    if [[ "${BEFORE_PROMPT[$field]}" != "${AFTER_PROMPT[$field]}" ]]; then
      echo "restore drill failed: active prompt manifest field mismatch: field=$field archive=${BEFORE_PROMPT[$field]} container=${AFTER_PROMPT[$field]}" >&2
      exit 1
    fi
  done
fi

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
