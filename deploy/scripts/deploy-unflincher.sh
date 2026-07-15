#!/usr/bin/env bash
# Deploy one prebuilt, revision-labeled image through the v0.1/v0.2 maintenance boundary.
# The live Quadlet remains untouched and continues to reference localhost/unflincher:latest.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

RELEASE_IMAGE="${UNFLINCHER_RELEASE_IMAGE:-}"
EXPECTED_REVISION="${UNFLINCHER_EXPECTED_REVISION:-}"
EXPECTED_VERSION="${UNFLINCHER_EXPECTED_VERSION:-}"
DEPLOY_MODE="${UNFLINCHER_DEPLOY_MODE:-}"
SECRET_NAME="${UNFLINCHER_COPILOT_SECRET:-unflincher-copilot-github-token}"
SERVICE_NAME="${UNFLINCHER_SERVICE_NAME:-unflincher.service}"
CONTAINER_NAME="${UNFLINCHER_CONTAINER_NAME:-unflincher}"
VOLUME_NAME="${UNFLINCHER_VOLUME_NAME:-unflincher-data}"
DB_PATH="${UNFLINCHER_DB_PATH:-/data/unflincher.db}"
LATEST_TAG="${UNFLINCHER_LATEST_TAG:-localhost/unflincher:latest}"
BASE_URL="${UNFLINCHER_BASE_URL:-http://127.0.0.1:8096}"
HEALTH_URL="${BASE_URL}/healthz"
STATE_DIR="${UNFLINCHER_DEPLOY_STATE_DIR:-$HOME/.local/state/unflincher-deploy}"
BACKUP_DIR="${UNFLINCHER_BACKUP_DIR:-$HOME/backups/unflincher}"
VERIFY_SCRIPT="${UNFLINCHER_BACKUP_VERIFY_SCRIPT:-$SCRIPT_DIR/verify-unflincher-backup.py}"
RESTORE_SCRIPT="${UNFLINCHER_RESTORE_SCRIPT:-$SCRIPT_DIR/unflincher-restore-drill.sh}"
DRAIN_ATTEMPTS="${UNFLINCHER_DRAIN_ATTEMPTS:-60}"
DRAIN_INTERVAL="${UNFLINCHER_DRAIN_INTERVAL_SECONDS:-2}"
HEALTH_ATTEMPTS="${UNFLINCHER_HEALTH_ATTEMPTS:-60}"
HEALTH_INTERVAL="${UNFLINCHER_HEALTH_INTERVAL_SECONDS:-1}"
ALTERNATE_PORT="${UNFLINCHER_ALTERNATE_PORT:-18096}"

TARGET_IMAGE_ID=""
TARGET_IMAGE_REVISION=""
TARGET_IMAGE_VERSION=""
PRIOR_IMAGE_ID=""
PRIOR_IMAGE_REVISION=""
PRIOR_IMAGE_VERSION=""
LATEST_IMAGE_ID=""
ROLLBACK_TAG=""
RUN_DIR=""
BACKUP_PATH=""
ENTRY_COUNT_BEFORE=""
PROMPT_MANIFEST_PATH=""
HEALTH_JSON=""
SERVICE_STOPPED=0
BACKUP_TEMP_DB=""
BACKUP_PARTIAL=""
LOCK_ACQUIRED=0
UNLOCK_FAILURE_LOCK_CONFIRMED=0

umask 077

die() {
  echo "deployment failed: $*" >&2
  exit 1
}

record_value() {
  local name="$1"
  local value="$2"
  printf '%s\n' "$value" > "$RUN_DIR/$name"
}

hash_file() {
  python3 -c \
    'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
    "$1"
}

image_id() {
  podman image inspect --format '{{.Id}}' "$1"
}

image_label() {
  local image="$1"
  local label="$2"
  podman image inspect --format "{{ index .Labels \"$label\" }}" "$image"
}

image_environment() {
  podman image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$1"
}

cleanup() {
  local status=$?
  if [[ -n "$BACKUP_TEMP_DB" ]]; then
    rm -f "$BACKUP_TEMP_DB"
  fi
  if [[ -n "$BACKUP_PARTIAL" ]]; then
    rm -f "$BACKUP_PARTIAL"
  fi
  if [[ "$LOCK_ACQUIRED" == "1" ]]; then
    rmdir "$STATE_DIR/deploy.lock" 2>/dev/null || true
  fi
  exit "$status"
}
trap cleanup EXIT

validate_inputs() {
  [[ -n "$RELEASE_IMAGE" ]] || die "UNFLINCHER_RELEASE_IMAGE is required"
  [[ "$EXPECTED_REVISION" =~ ^[0-9a-f]{40}$ ]] \
    || die "UNFLINCHER_EXPECTED_REVISION must be a lowercase 40-character Git commit SHA"
  [[ -n "$EXPECTED_VERSION" ]] || die "UNFLINCHER_EXPECTED_VERSION is required"
  [[ "$DEPLOY_MODE" == "first-upgrade" || "$DEPLOY_MODE" == "routine" ]] \
    || die "UNFLINCHER_DEPLOY_MODE must be first-upgrade or routine"
  [[ "$DRAIN_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || die "UNFLINCHER_DRAIN_ATTEMPTS must be positive"
  [[ "$DRAIN_INTERVAL" =~ ^[0-9]+$ ]] || die "UNFLINCHER_DRAIN_INTERVAL_SECONDS must be non-negative"
  [[ "$HEALTH_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || die "UNFLINCHER_HEALTH_ATTEMPTS must be positive"
  [[ "$HEALTH_INTERVAL" =~ ^[0-9]+$ ]] || die "UNFLINCHER_HEALTH_INTERVAL_SECONDS must be non-negative"
  [[ "$ALTERNATE_PORT" =~ ^[0-9]+$ ]] || die "UNFLINCHER_ALTERNATE_PORT must be numeric"
  [[ -x "$RESTORE_SCRIPT" ]] || die "restore drill is not executable: $RESTORE_SCRIPT"
  [[ -f "$VERIFY_SCRIPT" ]] || die "backup verifier is missing: $VERIFY_SCRIPT"
  for command in podman systemctl curl python3; do
    command -v "$command" >/dev/null 2>&1 || die "required command is missing: $command"
  done
}

acquire_deploy_lock() {
  mkdir -p "$STATE_DIR"
  chmod 0700 "$STATE_DIR"
  if ! mkdir "$STATE_DIR/deploy.lock" 2>/dev/null; then
    die "another deployment holds $STATE_DIR/deploy.lock"
  fi
  LOCK_ACQUIRED=1
}

capture_unit_snapshot() {
  local suffix="$1"
  systemctl --user cat "$SERVICE_NAME" > "$RUN_DIR/unit-${suffix}.txt" \
    || return 1
  hash_file "$RUN_DIR/unit-${suffix}.txt" > "$RUN_DIR/unit-${suffix}.sha256" \
    || return 1
}

preflight_images_and_service() {
  podman image exists "$RELEASE_IMAGE" \
    || die "release image is not present locally: $RELEASE_IMAGE"
  podman secret exists "$SECRET_NAME" 2>/dev/null \
    || die "missing Podman secret: $SECRET_NAME"
  systemctl --user is-active --quiet "$SERVICE_NAME" \
    || die "service is not active before deployment: $SERVICE_NAME"

  TARGET_IMAGE_ID="$(image_id "$RELEASE_IMAGE")"
  TARGET_IMAGE_REVISION="$(image_label "$RELEASE_IMAGE" "org.opencontainers.image.revision")"
  TARGET_IMAGE_VERSION="$(image_label "$RELEASE_IMAGE" "org.opencontainers.image.version")"
  [[ "$TARGET_IMAGE_REVISION" == "$EXPECTED_REVISION" ]] \
    || die "release image revision does not match UNFLINCHER_EXPECTED_REVISION"
  [[ "$TARGET_IMAGE_VERSION" == "$EXPECTED_VERSION" ]] \
    || die "release image version does not match UNFLINCHER_EXPECTED_VERSION"
  TARGET_IMAGE_ENVIRONMENT="$(image_environment "$RELEASE_IMAGE")"
  grep -Fxq "UNFLINCHER_REVISION=$EXPECTED_REVISION" <<< "$TARGET_IMAGE_ENVIRONMENT" \
    || die "release image runtime revision does not match its OCI label"
  grep -Fxq "UNFLINCHER_VERSION=$EXPECTED_VERSION" <<< "$TARGET_IMAGE_ENVIRONMENT" \
    || die "release image runtime version does not match its OCI label"

  PRIOR_IMAGE_ID="$(podman container inspect --format '{{.Image}}' "$CONTAINER_NAME")"
  LATEST_IMAGE_ID="$(image_id "$LATEST_TAG")"
  [[ "$PRIOR_IMAGE_ID" == "$LATEST_IMAGE_ID" ]] \
    || die "running container image does not match $LATEST_TAG"
  PRIOR_IMAGE_REVISION="$(image_label "$PRIOR_IMAGE_ID" "org.opencontainers.image.revision")"
  PRIOR_IMAGE_VERSION="$(image_label "$PRIOR_IMAGE_ID" "org.opencontainers.image.version")"
  if [[ "$DEPLOY_MODE" == "routine" ]]; then
    [[ "$PRIOR_IMAGE_REVISION" =~ ^[0-9a-f]{40}$ ]] \
      || die "routine deployment requires a revision-labeled prior v0.2 image"
    [[ -n "$PRIOR_IMAGE_VERSION" ]] \
      || die "routine deployment requires a version-labeled prior v0.2 image"
  fi

  local stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  RUN_DIR="$STATE_DIR/${stamp}-${EXPECTED_REVISION:0:12}"
  mkdir "$RUN_DIR"
  chmod 0700 "$RUN_DIR"
  ROLLBACK_TAG="localhost/unflincher:rollback-${EXPECTED_REVISION:0:12}-${stamp}"

  record_value mode "$DEPLOY_MODE"
  record_value release-image "$RELEASE_IMAGE"
  record_value target-image-id "$TARGET_IMAGE_ID"
  record_value target-revision "$TARGET_IMAGE_REVISION"
  record_value target-version "$TARGET_IMAGE_VERSION"
  record_value prior-image-id "$PRIOR_IMAGE_ID"
  record_value prior-revision "${PRIOR_IMAGE_REVISION:-unknown}"
  record_value prior-version "${PRIOR_IMAGE_VERSION:-unknown}"
  record_value rollback-tag "$ROLLBACK_TAG"
  record_value status preflight
  printf '%s\n' "$RUN_DIR" > "$STATE_DIR/latest-run"
  capture_unit_snapshot before
}

cli_live() {
  podman exec "$CONTAINER_NAME" python -m unflincher.cli "$@"
}

cli_offline() {
  local image="$1"
  shift
  podman run --rm --pull=never \
    -v "${VOLUME_NAME}:/data:Z" \
    "$image" \
    python -m unflincher.cli "$@"
}

offline_sql() {
  local sql="$1"
  podman run --rm --pull=never \
    --entrypoint sqlite3 \
    -v "${VOLUME_NAME}:/data:Z" \
    "$RELEASE_IMAGE" \
    -readonly \
    "$DB_PATH" \
    "$sql"
}

maintenance_status() {
  cli_live maintenance status --db "$DB_PATH" --json
}

lock_maintenance() {
  cli_live maintenance lock --db "$DB_PATH" --json > "$RUN_DIR/maintenance-lock.json"
}

status_is_drained() {
  STATUS_JSON="$1" python3 - <<'PY'
import json
import os
import sys

payload = json.loads(os.environ["STATUS_JSON"])
maintenance = payload["maintenance"]
sys.exit(
    0
    if maintenance["locked"]
    and maintenance["active_lease_count"] == 0
    and maintenance["running_regen_job_ids"] == []
    else 1
)
PY
}

status_reports_locked() {
  STATUS_JSON="$1" python3 - <<'PY'
import json
import os
import sys

payload = json.loads(os.environ["STATUS_JSON"])
sys.exit(0 if payload["maintenance"]["locked"] else 1)
PY
}

wait_for_drain() {
  local status
  for ((attempt = 1; attempt <= DRAIN_ATTEMPTS; attempt++)); do
    status="$(maintenance_status)"
    printf '%s\n' "$status" > "$RUN_DIR/maintenance-drain.json"
    if status_is_drained "$status"; then
      return 0
    fi
    sleep "$DRAIN_INTERVAL"
  done
  return 1
}

wait_for_health() {
  for ((attempt = 1; attempt <= HEALTH_ATTEMPTS; attempt++)); do
    if HEALTH_JSON="$(curl -fsS "$HEALTH_URL" 2>/dev/null)"; then
      return 0
    fi
    sleep "$HEALTH_INTERVAL"
  done
  return 1
}

assert_health_identity() {
  local expected_revision="$1"
  local expected_version="$2"
  local expected_locked="$3"
  HEALTH_JSON="$HEALTH_JSON" \
  EXPECTED_REVISION="$expected_revision" \
  EXPECTED_VERSION="$expected_version" \
  EXPECTED_LOCKED="$expected_locked" \
    python3 - <<'PY'
import json
import os
import sys

payload = json.loads(os.environ["HEALTH_JSON"])
expected_locked = os.environ["EXPECTED_LOCKED"] == "true"
ok = (
    payload.get("status") == "ok"
    and payload.get("revision") == os.environ["EXPECTED_REVISION"]
    and payload.get("version") == os.environ["EXPECTED_VERSION"]
    and payload.get("generation_locked") is expected_locked
)
sys.exit(0 if ok else 1)
PY
}

assert_status_matches_backup() {
  local status_json="$1"
  STATUS_JSON="$status_json" \
  PROMPT_MANIFEST_PATH="$PROMPT_MANIFEST_PATH" \
  ENTRY_COUNT_BEFORE="$ENTRY_COUNT_BEFORE" \
  CONTAINER_NAME="$CONTAINER_NAME" \
  DB_PATH="$DB_PATH" \
    python3 - <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path

payload = json.loads(os.environ["STATUS_JSON"])
state = payload["bootstrap_state"]
maintenance = payload["maintenance"]
if not (
    state["analyst_seeded"]
    and state["current_result_selection_verified"]
    and maintenance["locked"]
    and maintenance["active_lease_count"] == 0
    and maintenance["running_regen_job_ids"] == []
):
    raise SystemExit(1)

before = {}
for line in Path(os.environ["PROMPT_MANIFEST_PATH"]).read_text(encoding="utf-8").splitlines():
    field, value = line.split("=", 1)
    before[field] = value
after_row = payload["active_prompt"]
if not before:
    if after_row is not None:
        raise SystemExit(1)
else:
    if after_row is None:
        raise SystemExit(1)
    after = {
        field: "NULL" if after_row[field] is None else str(after_row[field])
        for field in (
            "id",
            "version_no",
            "body_sha256",
            "model",
            "is_active",
            "created_at",
            "preset_key",
        )
    }
    if before != after:
        raise SystemExit(1)

entry_count = subprocess.run(
    [
        "podman",
        "exec",
        os.environ["CONTAINER_NAME"],
        "sqlite3",
        os.environ["DB_PATH"],
        "SELECT COUNT(*) FROM diary_entry;",
    ],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
if entry_count != os.environ["ENTRY_COUNT_BEFORE"]:
    raise SystemExit(1)
PY
}

verify_crawler_defenses() {
  local headers="$RUN_DIR/crawler-headers.txt"
  local body="$RUN_DIR/robots.txt"
  curl -fsS -D "$headers" -o "$body" "${BASE_URL}/robots.txt"
  HEADERS_PATH="$headers" BODY_PATH="$body" python3 - <<'PY'
import os
from pathlib import Path

headers = Path(os.environ["HEADERS_PATH"]).read_text(encoding="utf-8").lower()
body = Path(os.environ["BODY_PATH"]).read_text(encoding="utf-8")
if "x-robots-tag: noindex, nofollow" not in headers:
    raise SystemExit(1)
if body != "User-agent: *\nDisallow: /\n":
    raise SystemExit(1)
PY
}

verify_locked_target() {
  local expected_image_id="$1"
  local expected_revision="$2"
  local expected_version="$3"
  local status_json
  local running_image_id
  local running_revision

  wait_for_health || return 1
  assert_health_identity "$expected_revision" "$expected_version" true || return 1
  printf '%s\n' "$HEALTH_JSON" > "$RUN_DIR/health-locked.json" \
    || return 1

  running_image_id="$(podman container inspect --format '{{.Image}}' "$CONTAINER_NAME")"
  [[ "$running_image_id" == "$expected_image_id" ]] || return 1
  running_revision="$(image_label "$running_image_id" "org.opencontainers.image.revision")"
  [[ "$running_revision" == "$expected_revision" ]] || return 1

  status_json="$(maintenance_status)"
  printf '%s\n' "$status_json" > "$RUN_DIR/maintenance-locked.json" \
    || return 1
  assert_status_matches_backup "$status_json" || return 1
  verify_crawler_defenses || return 1
}

run_deployment_probe() {
  local args=(probe)
  if [[ -n "${UNFLINCHER_PROBE_MODEL:-}" ]]; then
    args+=(--model "$UNFLINCHER_PROBE_MODEL")
  fi
  cli_live "${args[@]}" > "$RUN_DIR/deployment-probe.txt"
}

confirm_live_maintenance_lock() {
  local lock_json
  if ! lock_json="$(
    cli_live maintenance lock --db "$DB_PATH" --json
  )"; then
    return 1
  fi
  printf '%s\n' "$lock_json" > "$RUN_DIR/maintenance-relock.json" \
    || return 1
  status_reports_locked "$lock_json"
}

unlock_verified_target() {
  local unlock_json
  UNLOCK_FAILURE_LOCK_CONFIRMED=0
  if ! unlock_json="$(
    cli_live maintenance unlock --db "$DB_PATH" --confirm-service-healthy --json
  )"; then
    if confirm_live_maintenance_lock; then
      UNLOCK_FAILURE_LOCK_CONFIRMED=1
    fi
    return 1
  fi
  printf '%s\n' "$unlock_json" > "$RUN_DIR/maintenance-unlock.json" \
    || return 1
  if ! STATUS_JSON="$unlock_json" python3 - <<'PY'
import json
import os

payload = json.loads(os.environ["STATUS_JSON"])
if payload["maintenance"]["locked"]:
    raise SystemExit(1)
PY
  then
    if confirm_live_maintenance_lock; then
      UNLOCK_FAILURE_LOCK_CONFIRMED=1
    fi
    return 1
  fi
  if ! wait_for_health \
    || ! assert_health_identity "$EXPECTED_REVISION" "$EXPECTED_VERSION" false; then
    if confirm_live_maintenance_lock; then
      UNLOCK_FAILURE_LOCK_CONFIRMED=1
    fi
    return 1
  fi
  printf '%s\n' "$HEALTH_JSON" > "$RUN_DIR/health-unlocked.json" \
    || return 1
}

create_offline_backup() {
  local stamp
  local base
  local backup_name
  local temp_name

  mkdir -p "$BACKUP_DIR"
  chmod 0700 "$BACKUP_DIR"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  base="unflincher-pre-${DEPLOY_MODE}-${stamp}-$$"
  backup_name="${base}.db.gz"
  temp_name=".${base}.db"
  BACKUP_TEMP_DB="$BACKUP_DIR/$temp_name"
  BACKUP_PARTIAL="$BACKUP_DIR/.${backup_name}.partial"
  BACKUP_PATH="$BACKUP_DIR/$backup_name"

  podman run --rm --pull=never \
    --entrypoint sqlite3 \
    -v "${VOLUME_NAME}:/data:Z" \
    -v "${BACKUP_DIR}:/backup:Z" \
    "$RELEASE_IMAGE" \
    -readonly \
    "$DB_PATH" \
    ".backup '/backup/${temp_name}'" \
    || return 1
  podman run --rm --pull=never \
    --entrypoint gzip \
    -v "${BACKUP_DIR}:/backup:Z" \
    "$RELEASE_IMAGE" \
    -c "/backup/${temp_name}" > "$BACKUP_PARTIAL" \
    || return 1

  ENTRY_COUNT_BEFORE="$(python3 "$VERIFY_SCRIPT" "$BACKUP_PARTIAL")" \
    || return 1
  [[ "$ENTRY_COUNT_BEFORE" =~ ^[0-9]+$ ]] || return 1
  python3 "$VERIFY_SCRIPT" "$BACKUP_PARTIAL" --manifest \
    > "$RUN_DIR/pre-deploy-manifest.txt" \
    || return 1
  PROMPT_MANIFEST_PATH="$RUN_DIR/pre-deploy-prompt-manifest.txt"
  python3 "$VERIFY_SCRIPT" "$BACKUP_PARTIAL" --active-prompt-manifest \
    > "$PROMPT_MANIFEST_PATH" \
    || return 1

  mv "$BACKUP_PARTIAL" "$BACKUP_PATH" || return 1
  BACKUP_PARTIAL=""
  chmod 0600 "$BACKUP_PATH" || return 1
  rm -f "$BACKUP_TEMP_DB"
  BACKUP_TEMP_DB=""
  record_value pristine-backup-path "$BACKUP_PATH" || return 1
  record_value pre-deploy-entry-count "$ENTRY_COUNT_BEFORE" || return 1
}

preserve_rollback_image() {
  podman tag "$PRIOR_IMAGE_ID" "$ROLLBACK_TAG" || return 1
  [[ "$(image_id "$ROLLBACK_TAG")" == "$PRIOR_IMAGE_ID" ]]
}

run_restore_drill() {
  local image="$1"
  local port="$2"
  local bootstrap="$3"
  UNFLINCHER_RESTORE_IMAGE="$image" \
  UNFLINCHER_RESTORE_PORT="$port" \
  UNFLINCHER_RESTORE_BOOTSTRAP="$bootstrap" \
  UNFLINCHER_BACKUP_VERIFY_SCRIPT="$VERIFY_SCRIPT" \
    "$RESTORE_SCRIPT" "$BACKUP_PATH" "$ENTRY_COUNT_BEFORE"
}

record_and_compare_unit_after() {
  capture_unit_snapshot after || return 1
  local before_hash
  local after_hash
  before_hash="$(<"$RUN_DIR/unit-before.sha256")"
  after_hash="$(<"$RUN_DIR/unit-after.sha256")"
  [[ "$before_hash" == "$after_hash" ]]
}

restart_prior_after_unchanged_failure() {
  local reason="$1"
  local running_image_id=""
  echo "deployment failed before live database mutation: $reason" >&2
  if [[ "$SERVICE_STOPPED" == "1" ]]; then
    if ! systemctl --user start "$SERVICE_NAME"; then
      record_value status failed-before-mutation-service-down
      die "prior service failed to restart; production database is unchanged"
    fi
    SERVICE_STOPPED=0
  fi
  if ! wait_for_health; then
    record_value status failed-before-mutation-service-down
    die "prior service restarted but did not become healthy; production database is unchanged"
  fi
  if ! running_image_id="$(
    podman container inspect --format '{{.Image}}' "$CONTAINER_NAME"
  )" || [[ "$running_image_id" != "$PRIOR_IMAGE_ID" ]]; then
    record_value status failed-before-mutation-service-down
    die "prior service image identity could not be verified; production database is unchanged"
  fi
  record_value status failed-before-mutation
  echo "prior service restored and healthy; production database is unchanged" >&2
  exit 1
}

print_first_upgrade_recovery() {
  local lock_message="$1"
  cat >&2 <<EOF
deployment failed after the live v0.2 bootstrap began. Production remains stopped.
$lock_message
The verified pre-upgrade backup is:
  $BACKUP_PATH
The verified rollback image is:
  $ROLLBACK_TAG

Manual destructive restore, approval required:
  systemctl --user stop $SERVICE_NAME
  restore_dir=\$(mktemp -d)
  gunzip -c '$BACKUP_PATH' > "\$restore_dir/unflincher.db"
  podman run --rm --pull=never --entrypoint sh \\
    -v '${VOLUME_NAME}:/data:Z' -v "\$restore_dir:/restore:ro,Z" '$ROLLBACK_TAG' \\
    -c "rm -f '$DB_PATH' '${DB_PATH}-wal' '${DB_PATH}-shm' && cp /restore/unflincher.db '$DB_PATH'"
  podman tag '$ROLLBACK_TAG' '$LATEST_TAG'
  systemctl --user start '$SERVICE_NAME'
  curl -fsS '$HEALTH_URL'

Do not run these commands unless discarding every post-backup database change is approved.
EOF
}

fail_first_upgrade_locked() {
  local reason="$1"
  local lock_json=""
  local lock_message=""
  systemctl --user stop "$SERVICE_NAME" >/dev/null 2>&1 || true
  SERVICE_STOPPED=1
  podman tag "$TARGET_IMAGE_ID" "$LATEST_TAG" >/dev/null 2>&1 || true
  if lock_json="$(
    cli_offline "$RELEASE_IMAGE" maintenance lock --db "$DB_PATH" --json
  )" && status_reports_locked "$lock_json"; then
    printf '%s\n' "$lock_json" > "$RUN_DIR/maintenance-failure-lock.json"
    record_value status failed-locked
    lock_message="The maintenance lock was confirmed."
  else
    printf '%s\n' "$lock_json" > "$RUN_DIR/maintenance-failure-lock.json"
    record_value status failed-unlocked
    lock_message="CRITICAL: the maintenance lock could not be confirmed. Keep the service stopped."
  fi
  record_value failure-reason "$reason"
  print_first_upgrade_recovery "$lock_message"
  exit 1
}

fail_after_unlock_attempt() {
  local reason="$1"
  if [[ "$UNLOCK_FAILURE_LOCK_CONFIRMED" == "1" ]]; then
    record_value status failed-locked
    die "$reason; maintenance was re-locked"
  fi
  record_value status failed-unlocked
  die "CRITICAL: $reason; maintenance could not be confirmed locked"
}

recover_routine() {
  local reason="$1"
  echo "target deployment failed, attempting locked rollback: $reason" >&2
  podman tag "$PRIOR_IMAGE_ID" "$LATEST_TAG" || true
  if ! systemctl --user restart "$SERVICE_NAME"; then
    record_value status failed-locked
    die "rollback service restart failed; maintenance remains locked"
  fi
  SERVICE_STOPPED=0
  if ! verify_locked_target \
    "$PRIOR_IMAGE_ID" "$PRIOR_IMAGE_REVISION" "$PRIOR_IMAGE_VERSION"; then
    record_value status failed-locked
    die "rollback image did not pass locked verification; maintenance remains locked"
  fi
  EXPECTED_REVISION="$PRIOR_IMAGE_REVISION"
  EXPECTED_VERSION="$PRIOR_IMAGE_VERSION"
  if ! unlock_verified_target; then
    fail_after_unlock_attempt \
      "rollback image is healthy but maintenance unlock failed"
  fi
  record_value status rolled-back
  echo "deployment failed; prior revision restored and generation unlocked: $PRIOR_IMAGE_REVISION" >&2
  exit 1
}

recover_routine_before_backup() {
  local reason="$1"
  local status_json
  echo "deployment failed before a verified backup was created: $reason" >&2
  podman tag "$PRIOR_IMAGE_ID" "$LATEST_TAG" || true
  if ! systemctl --user restart "$SERVICE_NAME"; then
    record_value status failed-locked
    die "prior service restart failed; maintenance remains locked"
  fi
  SERVICE_STOPPED=0
  if ! wait_for_health \
    || ! assert_health_identity "$PRIOR_IMAGE_REVISION" "$PRIOR_IMAGE_VERSION" true; then
    record_value status failed-locked
    die "prior service did not return healthy; maintenance remains locked"
  fi
  status_json="$(maintenance_status)"
  if ! status_is_drained "$status_json"; then
    record_value status failed-locked
    die "prior service returned with active generation; maintenance remains locked"
  fi
  EXPECTED_REVISION="$PRIOR_IMAGE_REVISION"
  EXPECTED_VERSION="$PRIOR_IMAGE_VERSION"
  if ! unlock_verified_target; then
    fail_after_unlock_attempt \
      "prior service is healthy but maintenance unlock failed"
  fi
  record_value status rolled-back
  echo "deployment failed; unchanged prior service restored and generation unlocked" >&2
  exit 1
}

deploy_first_upgrade() {
  systemctl --user stop "$SERVICE_NAME"
  SERVICE_STOPPED=1

  local running_jobs
  if ! running_jobs="$(
    offline_sql "SELECT COUNT(*) FROM regen_job WHERE status = 'running';"
  )"; then
    restart_prior_after_unchanged_failure \
      "could not query v0.1 for running regeneration jobs"
  fi
  [[ "$running_jobs" == "0" ]] \
    || restart_prior_after_unchanged_failure "v0.1 has a running regeneration job"

  create_offline_backup \
    || restart_prior_after_unchanged_failure "could not create the pristine backup"
  preserve_rollback_image \
    || restart_prior_after_unchanged_failure "could not preserve the rollback image"
  run_restore_drill "$ROLLBACK_TAG" "$ALTERNATE_PORT" 0 \
    || restart_prior_after_unchanged_failure "the v0.1 rollback restore drill failed"
  run_restore_drill "$RELEASE_IMAGE" "$((ALTERNATE_PORT + 1))" 1 \
    || restart_prior_after_unchanged_failure "the v0.2 bootstrap restore drill failed"

  if ! cli_offline "$RELEASE_IMAGE" bootstrap --db "$DB_PATH" --json \
    > "$RUN_DIR/live-bootstrap.json"; then
    fail_first_upgrade_locked "offline bootstrap failed"
  fi

  podman tag "$TARGET_IMAGE_ID" "$LATEST_TAG" \
    || fail_first_upgrade_locked "could not repoint latest to the target image"
  systemctl --user start "$SERVICE_NAME" \
    || fail_first_upgrade_locked "target service did not start"
  SERVICE_STOPPED=0
  record_and_compare_unit_after \
    || fail_first_upgrade_locked "effective systemd unit changed during deployment"
  verify_locked_target "$TARGET_IMAGE_ID" "$EXPECTED_REVISION" "$EXPECTED_VERSION" \
    || fail_first_upgrade_locked "target failed locked verification"
  run_deployment_probe \
    || fail_first_upgrade_locked "local synthetic model probe failed"
  unlock_verified_target \
    || fail_first_upgrade_locked "verified target could not be unlocked"
}

deploy_routine() {
  lock_maintenance
  if ! wait_for_drain; then
    record_value status failed-locked
    die "maintenance drain timed out; old service remains running and generation stays locked"
  fi

  systemctl --user stop "$SERVICE_NAME"
  SERVICE_STOPPED=1
  create_offline_backup \
    || recover_routine_before_backup "could not create the pre-deploy backup"
  preserve_rollback_image || recover_routine "could not preserve the rollback image"
  run_restore_drill "$ROLLBACK_TAG" "$ALTERNATE_PORT" 0 \
    || recover_routine "rollback restore drill failed"
  run_restore_drill "$RELEASE_IMAGE" "$((ALTERNATE_PORT + 1))" 0 \
    || recover_routine "target restore drill failed"

  podman tag "$TARGET_IMAGE_ID" "$LATEST_TAG" \
    || recover_routine "could not repoint latest to the target image"
  systemctl --user start "$SERVICE_NAME" \
    || recover_routine "target service did not start"
  SERVICE_STOPPED=0
  record_and_compare_unit_after \
    || recover_routine "effective systemd unit changed during deployment"
  verify_locked_target "$TARGET_IMAGE_ID" "$EXPECTED_REVISION" "$EXPECTED_VERSION" \
    || recover_routine "target failed locked verification"
  run_deployment_probe \
    || recover_routine "local synthetic model probe failed"
  unlock_verified_target \
    || fail_after_unlock_attempt "verified target could not be unlocked"
}

main() {
  cd "$ROOT"
  validate_inputs
  acquire_deploy_lock
  preflight_images_and_service

  if [[ "$DEPLOY_MODE" == "first-upgrade" ]]; then
    deploy_first_upgrade
  else
    deploy_routine
  fi

  record_value status success
  echo "unflincher.service deployed: revision=$EXPECTED_REVISION version=$EXPECTED_VERSION"
  echo "state recorded in $RUN_DIR"
}

main "$@"
