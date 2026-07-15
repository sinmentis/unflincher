#!/usr/bin/env bash
# Build one SHA-tagged release image from an exact clean commit. This never changes latest.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REVISION="${UNFLINCHER_RELEASE_REVISION:-}"
VERSION="${UNFLINCHER_RELEASE_VERSION:-}"

die() {
  echo "release image build failed: $*" >&2
  exit 1
}

[[ "$REVISION" =~ ^[0-9a-f]{40}$ ]] \
  || die "UNFLINCHER_RELEASE_REVISION must be a lowercase 40-character Git commit SHA"
[[ -n "$VERSION" ]] || die "UNFLINCHER_RELEASE_VERSION is required"

cd "$ROOT"
git cat-file -e "${REVISION}^{commit}" 2>/dev/null \
  || die "release revision is not a local commit: $REVISION"
[[ "$(git rev-parse HEAD)" == "$REVISION" ]] \
  || die "HEAD does not equal UNFLINCHER_RELEASE_REVISION"
[[ -z "$(git status --porcelain --untracked-files=all)" ]] \
  || die "working tree must be completely clean"

PROJECT_VERSION="$(
  python3 - <<'PY'
import tomllib
from pathlib import Path

print(tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]["version"])
PY
)"
[[ "$PROJECT_VERSION" == "$VERSION" ]] \
  || die "pyproject.toml version $PROJECT_VERSION does not equal $VERSION"

IMAGE="${UNFLINCHER_RELEASE_IMAGE:-localhost/unflincher:${REVISION}}"
CREATED="$(git show -s --format=%cI "$REVISION")"

podman build --pull=never \
  --build-arg "UNFLINCHER_REVISION=$REVISION" \
  --build-arg "UNFLINCHER_VERSION=$VERSION" \
  --build-arg "UNFLINCHER_BUILD_CREATED=$CREATED" \
  -t "$IMAGE" \
  .

ACTUAL_REVISION="$(
  podman image inspect --format \
    '{{ index .Labels "org.opencontainers.image.revision" }}' "$IMAGE"
)"
ACTUAL_VERSION="$(
  podman image inspect --format \
    '{{ index .Labels "org.opencontainers.image.version" }}' "$IMAGE"
)"
[[ "$ACTUAL_REVISION" == "$REVISION" ]] \
  || die "built image revision label does not match"
[[ "$ACTUAL_VERSION" == "$VERSION" ]] \
  || die "built image version label does not match"
IMAGE_ENVIRONMENT="$(
  podman image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$IMAGE"
)"
grep -Fxq "UNFLINCHER_REVISION=$REVISION" <<< "$IMAGE_ENVIRONMENT" \
  || die "built image runtime revision does not match"
grep -Fxq "UNFLINCHER_VERSION=$VERSION" <<< "$IMAGE_ENVIRONMENT" \
  || die "built image runtime version does not match"

IMAGE_ID="$(podman image inspect --format '{{.Id}}' "$IMAGE")"
echo "release image built: image=$IMAGE id=$IMAGE_ID revision=$REVISION version=$VERSION"
