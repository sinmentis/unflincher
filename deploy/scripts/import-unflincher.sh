#!/bin/bash
# deploy/scripts/import-unflincher.sh
# Run ONCE, before unflincher.service is first started, so there are never two writers to the
# SQLite file (the app itself only ever runs one writer; a concurrent import would race it).
# Usage:
#   deploy/scripts/import-unflincher.sh /path/to/your-export.xlsx
set -euo pipefail
XLSX_PATH="${1:?usage: import-unflincher.sh /path/to/export.xlsx}"

# Use a scratch dir under this repo checkout, not a fixed personal path, so this works
# regardless of where you cloned the repo.
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
IMPORT_DIR="$REPO_ROOT/import"
mkdir -p "$IMPORT_DIR"
cp "$XLSX_PATH" "$IMPORT_DIR/"
XLSX_NAME=$(basename "$XLSX_PATH")

podman volume create unflincher-data >/dev/null 2>&1 || true
podman run --rm \
  --volume unflincher-data:/data \
  --volume "$IMPORT_DIR:/import:ro,z" \
  localhost/unflincher:latest \
  python -m unflincher.cli import --excel "/import/${XLSX_NAME}" --db /data/unflincher.db

echo "Verifying row count:"
podman run --rm --volume unflincher-data:/data localhost/unflincher:latest \
  sqlite3 /data/unflincher.db "select count(*) from diary_entry;"
