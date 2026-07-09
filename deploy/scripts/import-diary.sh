#!/bin/bash
# deploy/scripts/import-diary.sh
# Run ONCE, before diary.service is first started, so there are never two writers to the
# SQLite file (technical design §7.4/§2.3). Usage:
#   deploy/scripts/import-diary.sh /path/to/豆伴export.xlsx
set -euo pipefail
XLSX_PATH="${1:?usage: import-diary.sh /path/to/export.xlsx}"

mkdir -p "$HOME/work/website/diary/import"
cp "$XLSX_PATH" "$HOME/work/website/diary/import/"
XLSX_NAME=$(basename "$XLSX_PATH")

podman volume create diary-data >/dev/null 2>&1 || true
podman run --rm \
  --volume diary-data:/data \
  --volume "$HOME/work/website/diary/import:/import:ro,z" \
  localhost/diary:latest \
  python -m diary.cli import --excel "/import/${XLSX_NAME}" --db /data/diary.db

echo "Verifying row count:"
podman run --rm --volume diary-data:/data localhost/diary:latest \
  sqlite3 /data/diary.db "select count(*) from diary_entry;"
