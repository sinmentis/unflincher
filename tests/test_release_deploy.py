import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "deploy/scripts/deploy-unflincher.sh"
BUILD_SCRIPT = ROOT / "deploy/scripts/build-unflincher-release.sh"
RESTORE_SCRIPT = ROOT / "deploy/scripts/unflincher-restore-drill.sh"


def test_deploy_consumes_a_prebuilt_exact_revision_image():
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "podman build" not in text
    assert "UNFLINCHER_RELEASE_IMAGE" in text
    assert "UNFLINCHER_EXPECTED_REVISION" in text
    assert "org.opencontainers.image.revision" in text
    assert "podman image exists" in text


def test_deploy_requires_an_explicit_version_boundary_mode():
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "UNFLINCHER_DEPLOY_MODE" in text
    assert "first-upgrade" in text
    assert "routine" in text
    assert "auto" not in text


def test_deploy_records_rollback_and_effective_unit_identity():
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "ROLLBACK_TAG" in text
    assert 'podman tag "$PRIOR_IMAGE_ID" "$ROLLBACK_TAG"' in text
    assert "systemctl --user cat" in text
    assert "unit-before.sha256" in text
    assert "unit-after.sha256" in text


def test_deploy_verifies_locked_target_before_unlocking():
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    verification = text.index("verify_locked_target")
    probe = text.index("run_deployment_probe", verification)
    unlock = text.index("unlock_verified_target", probe)

    assert verification < probe < unlock


def test_first_upgrade_keeps_destructive_restore_manual():
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "print_first_upgrade_recovery" in text
    assert "approval required" in text.lower()
    assert "gunzip -c" in text
    assert "failed-locked" in text


def test_restore_drill_can_bootstrap_only_the_disposable_copy():
    text = RESTORE_SCRIPT.read_text(encoding="utf-8")

    assert "UNFLINCHER_RESTORE_BOOTSTRAP" in text
    assert "python -m unflincher.cli bootstrap" in text
    assert text.index("python -m unflincher.cli bootstrap") < text.index("podman run -d")


def test_release_build_requires_a_clean_exact_commit_and_never_tags_latest():
    text = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "UNFLINCHER_RELEASE_REVISION" in text
    assert "UNFLINCHER_RELEASE_VERSION" in text
    assert "git status --porcelain" in text
    assert "git rev-parse HEAD" in text
    assert "org.opencontainers.image.revision" in text
    assert "org.opencontainers.image.version" in text
    assert "--pull=never" in text
    assert "localhost/unflincher:latest" not in text


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _write_fake_commands(tmp_path: Path) -> dict[str, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    paths = {
        "log": tmp_path / "commands.log",
        "restore_log": tmp_path / "restore.log",
        "latest": tmp_path / "latest-image",
        "container": tmp_path / "container-image",
        "maintenance": tmp_path / "maintenance-locked",
        "status_count": tmp_path / "status-count",
        "health_count": tmp_path / "health-count",
        "unlock_marker": tmp_path / "unlock-succeeded",
        "state": tmp_path / "state",
        "backups": tmp_path / "backups",
        "verifier": tmp_path / "verify-backup",
        "restore": tmp_path / "restore-drill",
    }
    paths["latest"].write_text("prior-image-id\n", encoding="utf-8")
    paths["container"].write_text("prior-image-id\n", encoding="utf-8")
    paths["maintenance"].write_text("0\n", encoding="utf-8")
    paths["status_count"].write_text("0\n", encoding="utf-8")
    paths["health_count"].write_text("0\n", encoding="utf-8")

    _write_executable(
        fake_bin / "podman",
        r"""#!/usr/bin/env bash
set -euo pipefail
printf 'podman %s\n' "$*" >> "$FAKE_COMMAND_LOG"

image_kind() {
  if [[ "$1" == "$FAKE_TARGET_REF" || "$1" == "$FAKE_TARGET_ID" ]]; then
    printf 'target\n'
  else
    printf 'prior\n'
  fi
}

emit_status() {
  local locked
  local count
  local active=0
  read -r locked < "$FAKE_MAINTENANCE_FILE"
  read -r count < "$FAKE_STATUS_COUNT"
  count=$((count + 1))
  printf '%s\n' "$count" > "$FAKE_STATUS_COUNT"
  if (( count <= FAKE_DRAIN_BUSY_COUNT )); then
    active=1
  fi
  printf '{"active_prompt":{"id":1,"version_no":1,"body_sha256":"abc","model":"gpt-5.4","is_active":1,"created_at":"2026-01-01T00:00:00+00:00","preset_key":null},"bootstrap_state":{"analyst_seeded":true,"current_result_selection_verified":true,"is_fresh_install":false},"maintenance":{"active_lease_count":%s,"locked":%s,"running_regen_job_ids":[]}}\n' "$active" "$([[ "$locked" == "1" ]] && printf true || printf false)"
}

if [[ "$1" == "image" && "$2" == "exists" ]]; then
  exit "${FAKE_IMAGE_MISSING:-0}"
fi
if [[ "$1" == "secret" && "$2" == "exists" ]]; then
  exit 0
fi
if [[ "$1" == "image" && "$2" == "inspect" ]]; then
  template="$4"
  image="$5"
  kind="$(image_kind "$image")"
  if [[ "$template" == *".Id"* ]]; then
    if [[ "$image" == "$FAKE_LATEST_TAG" ]]; then
      cat "$FAKE_LATEST_FILE"
    elif [[ "$kind" == "target" ]]; then
      printf '%s\n' "$FAKE_TARGET_ID"
    else
      printf '%s\n' "$FAKE_PRIOR_ID"
    fi
  elif [[ "$template" == *".Config.Env"* ]]; then
    if [[ "$kind" == "target" ]]; then
      printf 'UNFLINCHER_REVISION=%s\nUNFLINCHER_VERSION=%s\n' "$FAKE_TARGET_REVISION" "$FAKE_TARGET_VERSION"
    else
      printf 'UNFLINCHER_REVISION=%s\nUNFLINCHER_VERSION=%s\n' "$FAKE_PRIOR_REVISION" "$FAKE_PRIOR_VERSION"
    fi
  elif [[ "$template" == *"revision"* ]]; then
    [[ "$kind" == "target" ]] && printf '%s\n' "$FAKE_TARGET_REVISION" || printf '%s\n' "$FAKE_PRIOR_REVISION"
  elif [[ "$template" == *"version"* ]]; then
    [[ "$kind" == "target" ]] && printf '%s\n' "$FAKE_TARGET_VERSION" || printf '%s\n' "$FAKE_PRIOR_VERSION"
  fi
  exit 0
fi
if [[ "$1" == "container" && "$2" == "inspect" ]]; then
  cat "$FAKE_CONTAINER_FILE"
  exit 0
fi
if [[ "$1" == "tag" ]]; then
  source="$2"
  destination="$3"
  if [[ "$destination" == "$FAKE_LATEST_TAG" ]]; then
    if [[ "$source" == "$FAKE_TARGET_ID" || "$source" == "$FAKE_TARGET_REF" ]]; then
      printf '%s\n' "$FAKE_TARGET_ID" > "$FAKE_LATEST_FILE"
    else
      printf '%s\n' "$FAKE_PRIOR_ID" > "$FAKE_LATEST_FILE"
    fi
  fi
  exit 0
fi
if [[ "$1" == "exec" ]]; then
  joined="$*"
  if [[ "$joined" == *"maintenance lock"* ]]; then
    if [[ -f "$FAKE_UNLOCK_MARKER" && "${FAKE_RELOCK_FAIL:-0}" == "1" ]]; then
      exit 1
    fi
    printf '1\n' > "$FAKE_MAINTENANCE_FILE"
    emit_status
  elif [[ "$joined" == *"maintenance unlock"* ]]; then
    if [[ "${FAKE_UNLOCK_FAIL:-0}" == "1" ]]; then
      exit 1
    fi
    printf '0\n' > "$FAKE_MAINTENANCE_FILE"
    : > "$FAKE_UNLOCK_MARKER"
    emit_status
  elif [[ "$joined" == *"maintenance status"* ]]; then
    emit_status
  elif [[ "$joined" == *"unflincher.cli probe"* ]]; then
    [[ "${FAKE_PROBE_FAIL:-0}" == "0" ]]
    printf 'Deployment probe ok\n'
  elif [[ "$joined" == *"SELECT COUNT(*) FROM diary_entry"* ]]; then
    printf '2\n'
  fi
  exit 0
fi
if [[ "$1" == "run" ]]; then
  joined="$*"
  if [[ "$joined" == *"SELECT COUNT(*) FROM regen_job WHERE status = 'running'"* ]]; then
    if [[ "${FAKE_RUNNING_QUERY_FAIL:-0}" == "1" ]]; then
      exit 1
    fi
    printf '%s\n' "${FAKE_RUNNING_JOBS:-0}"
  elif [[ "$joined" == *"unflincher.cli bootstrap"* ]]; then
    printf '1\n' > "$FAKE_MAINTENANCE_FILE"
    [[ "${FAKE_BOOTSTRAP_FAIL:-0}" == "0" ]]
    emit_status
  elif [[ "$joined" == *"maintenance lock"* ]]; then
    if [[ -f "$FAKE_UNLOCK_MARKER" && "${FAKE_RELOCK_FAIL:-0}" == "1" ]]; then
      exit 1
    fi
    printf '1\n' > "$FAKE_MAINTENANCE_FILE"
    emit_status
  elif [[ "$joined" == *"--entrypoint gzip"* ]]; then
    printf 'fake-backup'
  fi
  exit 0
fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "systemctl",
        r"""#!/usr/bin/env bash
set -euo pipefail
printf 'systemctl %s\n' "$*" >> "$FAKE_COMMAND_LOG"
if [[ "$*" == *" cat "* ]]; then
  printf '[Service]\nEnvironment=PRIVATE_VALUE\n'
elif [[ "$*" == *" start "* || "$*" == *" restart "* ]]; then
  if [[ "$*" == *" start "* && "${FAKE_START_FAIL:-0}" == "1" ]]; then
    exit 1
  fi
  cp "$FAKE_LATEST_FILE" "$FAKE_CONTAINER_FILE"
fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "curl",
        r"""#!/usr/bin/env bash
set -euo pipefail
printf 'curl %s\n' "$*" >> "$FAKE_COMMAND_LOG"
headers=""
body=""
url="${!#}"
while (( $# > 0 )); do
  case "$1" in
    -D)
      headers="$2"
      shift 2
      ;;
    -o)
      body="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done
if [[ "$url" == */robots.txt ]]; then
  printf 'HTTP/1.1 200 OK\r\nX-Robots-Tag: noindex, nofollow\r\n\r\n' > "$headers"
  printf 'User-agent: *\nDisallow: /\n' > "$body"
  exit 0
fi
read -r current < "$FAKE_CONTAINER_FILE"
read -r locked < "$FAKE_MAINTENANCE_FILE"
read -r count < "$FAKE_HEALTH_COUNT"
count=$((count + 1))
printf '%s\n' "$count" > "$FAKE_HEALTH_COUNT"
if [[ "$current" == "$FAKE_TARGET_ID" ]]; then
  if [[ "${FAKE_TARGET_HEALTH_FAIL:-0}" == "1" ]]; then
    exit 56
  fi
  if (( count <= FAKE_HEALTH_TRANSIENT_FAILURES )); then
    exit 56
  fi
  revision="$FAKE_TARGET_REVISION"
  version="$FAKE_TARGET_VERSION"
else
  revision="$FAKE_PRIOR_REVISION"
  version="$FAKE_PRIOR_VERSION"
fi
if [[ "$locked" == "0" && "${FAKE_HEALTH_FAIL_WHEN_UNLOCKED:-0}" == "1" ]]; then
  exit 56
fi
printf '{"status":"ok","revision":"%s","version":"%s","generation_locked":%s}\n' \
  "$revision" "$version" "$([[ "$locked" == "1" ]] && printf true || printf false)"
""",
    )
    _write_executable(
        fake_bin / "sleep",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    _write_executable(
        paths["verifier"],
        r"""#!/usr/bin/env python3
import os
import sys

if "--active-prompt-manifest" in sys.argv:
    print("id=1")
    print("version_no=1")
    print("body_sha256=abc")
    print("model=gpt-5.4")
    print("is_active=1")
    print("created_at=2026-01-01T00:00:00+00:00")
    print("preset_key=NULL")
elif "--manifest" in sys.argv:
    print("diary_entry=2")
else:
    if os.environ.get("FAKE_VERIFY_ENTRY_COUNT_FAIL") == "1":
        raise SystemExit(1)
    print("2")
""",
    )
    _write_executable(
        paths["restore"],
        r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s|%s|%s\n' "$UNFLINCHER_RESTORE_IMAGE" "$UNFLINCHER_RESTORE_PORT" "$UNFLINCHER_RESTORE_BOOTSTRAP" >> "$FAKE_RESTORE_LOG"
if [[ "$UNFLINCHER_RESTORE_IMAGE" == "$FAKE_TARGET_REF" && "${FAKE_TARGET_DRILL_FAIL:-0}" == "1" ]]; then
  exit 1
fi
exit 0
""",
    )
    paths["bin"] = fake_bin
    return paths


def _run_deploy(
    tmp_path: Path,
    *,
    mode: str,
    overrides: dict[str, str] | None = None,
    precreate_lock: bool = False,
) -> tuple[subprocess.CompletedProcess, dict[str, Path]]:
    paths = _write_fake_commands(tmp_path)
    target_revision = "a" * 40
    prior_revision = "b" * 40
    target_ref = f"localhost/unflincher:{target_revision}"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{paths['bin']}:{env['PATH']}",
            "FAKE_COMMAND_LOG": str(paths["log"]),
            "FAKE_RESTORE_LOG": str(paths["restore_log"]),
            "FAKE_LATEST_FILE": str(paths["latest"]),
            "FAKE_CONTAINER_FILE": str(paths["container"]),
            "FAKE_MAINTENANCE_FILE": str(paths["maintenance"]),
            "FAKE_STATUS_COUNT": str(paths["status_count"]),
            "FAKE_HEALTH_COUNT": str(paths["health_count"]),
            "FAKE_UNLOCK_MARKER": str(paths["unlock_marker"]),
            "FAKE_TARGET_REF": target_ref,
            "FAKE_TARGET_ID": "target-image-id",
            "FAKE_PRIOR_ID": "prior-image-id",
            "FAKE_TARGET_REVISION": target_revision,
            "FAKE_PRIOR_REVISION": prior_revision,
            "FAKE_TARGET_VERSION": "0.2.0",
            "FAKE_PRIOR_VERSION": "0.1.0" if mode == "first-upgrade" else "0.1.1",
            "FAKE_LATEST_TAG": "localhost/unflincher:latest",
            "FAKE_DRAIN_BUSY_COUNT": "0",
            "FAKE_HEALTH_TRANSIENT_FAILURES": "0",
            "UNFLINCHER_RELEASE_IMAGE": target_ref,
            "UNFLINCHER_EXPECTED_REVISION": target_revision,
            "UNFLINCHER_EXPECTED_VERSION": "0.2.0",
            "UNFLINCHER_DEPLOY_MODE": mode,
            "UNFLINCHER_DEPLOY_STATE_DIR": str(paths["state"]),
            "UNFLINCHER_BACKUP_DIR": str(paths["backups"]),
            "UNFLINCHER_BACKUP_VERIFY_SCRIPT": str(paths["verifier"]),
            "UNFLINCHER_RESTORE_SCRIPT": str(paths["restore"]),
            "UNFLINCHER_DRAIN_ATTEMPTS": "2",
            "UNFLINCHER_DRAIN_INTERVAL_SECONDS": "0",
            "UNFLINCHER_HEALTH_ATTEMPTS": "2",
            "UNFLINCHER_HEALTH_INTERVAL_SECONDS": "0",
        }
    )
    if overrides:
        env.update(overrides)
    if precreate_lock:
        (paths["state"] / "deploy.lock").mkdir(parents=True)
    result = subprocess.run(
        ["bash", str(DEPLOY_SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    return result, paths


def _latest_run(paths: dict[str, Path]) -> Path:
    return Path((paths["state"] / "latest-run").read_text(encoding="utf-8").strip())


def test_routine_deploy_locks_drains_verifies_probes_then_unlocks(tmp_path):
    result, paths = _run_deploy(
        tmp_path,
        mode="routine",
        overrides={"FAKE_DRAIN_BUSY_COUNT": "1", "FAKE_HEALTH_TRANSIENT_FAILURES": "1"},
    )

    assert result.returncode == 0, result.stderr
    commands = paths["log"].read_text(encoding="utf-8")
    lock = commands.index("maintenance lock")
    rollback_tag = commands.index("podman tag prior-image-id localhost/unflincher:rollback-")
    target_tag = commands.index(
        "podman tag target-image-id localhost/unflincher:latest"
    )
    probe = commands.index("unflincher.cli probe")
    unlock = commands.index("maintenance unlock")
    assert lock < rollback_tag < target_tag < probe < unlock
    assert paths["restore_log"].read_text(encoding="utf-8").splitlines() == [
        next(
            line
            for line in paths["restore_log"].read_text(encoding="utf-8").splitlines()
            if line.endswith("|18096|0")
        ),
        f"localhost/unflincher:{'a' * 40}|18097|0",
    ]
    run_dir = _latest_run(paths)
    assert (run_dir / "status").read_text(encoding="utf-8").strip() == "success"
    assert (run_dir / "unit-before.sha256").read_text() == (
        run_dir / "unit-after.sha256"
    ).read_text()
    assert paths["maintenance"].read_text().strip() == "0"


def test_first_upgrade_drills_backup_then_bootstraps_before_retag(tmp_path):
    result, paths = _run_deploy(tmp_path, mode="first-upgrade")

    assert result.returncode == 0, result.stderr
    commands = paths["log"].read_text(encoding="utf-8")
    stop = commands.index("systemctl --user stop unflincher.service")
    running_check = commands.index("SELECT COUNT(*) FROM regen_job")
    live_bootstrap = commands.rindex("unflincher.cli bootstrap")
    target_tag = commands.index(
        "podman tag target-image-id localhost/unflincher:latest"
    )
    unlock = commands.index("maintenance unlock")
    assert stop < running_check < live_bootstrap < target_tag < unlock
    restore_lines = paths["restore_log"].read_text(encoding="utf-8").splitlines()
    assert restore_lines[0].endswith("|18096|0")
    assert restore_lines[1] == f"localhost/unflincher:{'a' * 40}|18097|1"
    assert "maintenance lock" not in commands
    assert (_latest_run(paths) / "status").read_text().strip() == "success"


def test_revision_mismatch_aborts_before_any_mutation(tmp_path):
    result, paths = _run_deploy(
        tmp_path,
        mode="routine",
        overrides={"FAKE_TARGET_REVISION": "c" * 40},
    )

    assert result.returncode == 1
    commands = paths["log"].read_text(encoding="utf-8")
    assert "revision does not match" in result.stderr
    assert "podman tag" not in commands
    assert "systemctl --user stop" not in commands
    assert "maintenance lock" not in commands


def test_routine_target_health_failure_restores_prior_and_unlocks(tmp_path):
    result, paths = _run_deploy(
        tmp_path,
        mode="routine",
        overrides={"FAKE_TARGET_HEALTH_FAIL": "1"},
    )

    assert result.returncode == 1
    commands = paths["log"].read_text(encoding="utf-8")
    target_tag = commands.index(
        "podman tag target-image-id localhost/unflincher:latest"
    )
    rollback_tag = commands.index(
        "podman tag prior-image-id localhost/unflincher:latest", target_tag
    )
    rollback_restart = commands.index(
        "systemctl --user restart unflincher.service", rollback_tag
    )
    unlock = commands.index("maintenance unlock", rollback_restart)
    assert target_tag < rollback_tag < rollback_restart < unlock
    assert (_latest_run(paths) / "status").read_text().strip() == "rolled-back"
    assert paths["latest"].read_text().strip() == "prior-image-id"
    assert paths["maintenance"].read_text().strip() == "0"


def test_first_upgrade_target_failure_stays_stopped_and_requires_manual_restore(
    tmp_path,
):
    result, paths = _run_deploy(
        tmp_path,
        mode="first-upgrade",
        overrides={"FAKE_TARGET_HEALTH_FAIL": "1"},
    )

    assert result.returncode == 1
    commands = paths["log"].read_text(encoding="utf-8")
    assert commands.rstrip().endswith(
        "systemctl --user stop unflincher.service"
    ) or "systemctl --user stop unflincher.service" in commands
    assert "approval required" in result.stderr
    assert "gunzip -c" in result.stderr
    assert (_latest_run(paths) / "status").read_text().strip() == "failed-locked"
    assert paths["latest"].read_text().strip() == "target-image-id"
    assert paths["maintenance"].read_text().strip() == "1"


def test_routine_drain_timeout_never_stops_or_retags_service(tmp_path):
    result, paths = _run_deploy(
        tmp_path,
        mode="routine",
        overrides={"FAKE_DRAIN_BUSY_COUNT": "10"},
    )

    assert result.returncode == 1
    commands = paths["log"].read_text(encoding="utf-8")
    assert "maintenance lock" in commands
    assert "systemctl --user stop" not in commands
    assert "podman tag" not in commands
    assert "generation stays locked" in result.stderr
    assert paths["maintenance"].read_text().strip() == "1"


def test_unlock_failure_cannot_be_reported_as_success(tmp_path):
    result, paths = _run_deploy(
        tmp_path,
        mode="routine",
        overrides={"FAKE_UNLOCK_FAIL": "1"},
    )

    assert result.returncode == 1
    run_dir = _latest_run(paths)
    assert (run_dir / "status").read_text().strip() == "failed-locked"
    assert paths["maintenance"].read_text().strip() == "1"
    assert "deployed: revision=" not in result.stdout


def test_failed_concurrent_lock_attempt_does_not_remove_the_owner_lock(tmp_path):
    result, paths = _run_deploy(
        tmp_path,
        mode="routine",
        precreate_lock=True,
    )

    assert result.returncode == 1
    assert "another deployment holds" in result.stderr
    assert (paths["state"] / "deploy.lock").is_dir()


def test_first_upgrade_query_failure_restarts_unchanged_v0_1(tmp_path):
    result, paths = _run_deploy(
        tmp_path,
        mode="first-upgrade",
        overrides={"FAKE_RUNNING_QUERY_FAIL": "1"},
    )

    assert result.returncode == 1
    commands = paths["log"].read_text(encoding="utf-8")
    stop = commands.index("systemctl --user stop unflincher.service")
    start = commands.index("systemctl --user start unflincher.service", stop)
    assert stop < start
    assert "podman tag" not in commands
    assert paths["latest"].read_text().strip() == "prior-image-id"


def test_backup_entry_count_failure_aborts_before_live_mutation(tmp_path):
    result, paths = _run_deploy(
        tmp_path,
        mode="first-upgrade",
        overrides={"FAKE_VERIFY_ENTRY_COUNT_FAIL": "1"},
    )

    assert result.returncode == 1
    commands = paths["log"].read_text(encoding="utf-8")
    assert "unflincher.cli bootstrap" not in commands
    assert "podman tag" not in commands
    assert "systemctl --user start unflincher.service" in commands
    assert paths["latest"].read_text().strip() == "prior-image-id"


def test_failed_emergency_relock_is_never_reported_as_failed_locked(tmp_path):
    result, paths = _run_deploy(
        tmp_path,
        mode="routine",
        overrides={
            "FAKE_HEALTH_FAIL_WHEN_UNLOCKED": "1",
            "FAKE_RELOCK_FAIL": "1",
        },
    )

    assert result.returncode == 1
    run_dir = _latest_run(paths)
    assert (run_dir / "status").read_text().strip() == "failed-unlocked"
    assert "could not be confirmed locked" in result.stderr
    assert paths["maintenance"].read_text().strip() == "0"


def test_first_upgrade_pre_mutation_restart_failure_is_reported_as_service_down(
    tmp_path,
):
    result, paths = _run_deploy(
        tmp_path,
        mode="first-upgrade",
        overrides={
            "FAKE_RUNNING_QUERY_FAIL": "1",
            "FAKE_START_FAIL": "1",
        },
    )

    assert result.returncode == 1
    run_dir = _latest_run(paths)
    assert (run_dir / "status").read_text().strip() == (
        "failed-before-mutation-service-down"
    )
    assert "prior service failed to restart" in result.stderr
