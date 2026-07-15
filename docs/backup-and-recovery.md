# Backups and recovery

The nightly timer `unflincher-backup.timer` runs `~/.local/bin/unflincher-backup.sh`. The script
uses SQLite online `.backup`, so it is safe while the WAL-mode application is running. It writes a
hidden `.partial` gzip first, then runs `verify-unflincher-backup.py`, which decompresses the
archive, runs `PRAGMA integrity_check`, and checks every application-table row count. It also
compares the backup entry count against the live count before atomically publishing
`unflincher-*.db.gz` with `0600` permissions. A failed check leaves no final artifact. Backups go to
`~/backups/unflincher/` by default and are pruned after 30 days; override with
`UNFLINCHER_BACKUP_DIR` and `UNFLINCHER_BACKUP_RETENTION_DAYS`.

These are still backups on the same host. A passing check or drill does not provide an off-host copy.

## Install the backup scripts

```bash
install -d -m 0755 ~/.local/bin
install -m 0755 \
  deploy/scripts/unflincher-backup.sh \
  deploy/scripts/verify-unflincher-backup.py \
  ~/.local/bin/
cp deploy/systemd/unflincher-backup.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now unflincher-backup.timer
```

## Test a backup without touching production

The disposable restore drill verifies all application-table row counts, starts the real image
against a uniquely named temporary volume on loopback port `18096`, and checks health plus the
timeline, report, chat, workshop, and one entry page. It never mounts the production
`unflincher-data` volume, and it removes the temporary container, volume, and database on success or
failure. Set `UNFLINCHER_RESTORE_PORT` if `18096` is occupied.

```bash
LATEST="$(ls -1t "$HOME"/backups/unflincher/unflincher-*.db.gz | head -1)"
EXPECTED="$(
  podman exec unflincher sqlite3 /data/unflincher.db \
    "SELECT COUNT(*) FROM diary_entry;"
)"
deploy/scripts/unflincher-restore-drill.sh "$LATEST" "$EXPECTED"
```

For a v0.1 backup tested with a v0.2 image, set `UNFLINCHER_RESTORE_BOOTSTRAP=1`. The offline
bootstrap runs only against the disposable restored copy before the temporary application starts.
The release deployment script uses this mode to prove the migration path without touching
production.

## Restore production

This overwrites the production database. Run the drill first, then take one final verified backup.
Run the commands in one shell and stop immediately if any command returns a non-zero status.

```bash
# 1. Preserve and verify the current production state.
PRE_RESTORE_DIR="$HOME/backups/unflincher/pre-restore"
UNFLINCHER_BACKUP_DIR="$PRE_RESTORE_DIR" ~/.local/bin/unflincher-backup.sh

# 2. Select and verify the artifact to restore.
RESTORE_ARCHIVE="$(ls -1t "$HOME"/backups/unflincher/unflincher-*.db.gz | head -1)"
RESTORE_COUNT="$(python3 ~/.local/bin/verify-unflincher-backup.py "$RESTORE_ARCHIVE")"

# 3. Stop writes and stage the verified database.
systemctl --user stop unflincher.service
RESTORE_TMP="$(mktemp -d)"
gunzip -c "$RESTORE_ARCHIVE" > "$RESTORE_TMP/unflincher.db"

# 4. Replace the database and clear the WAL and SHM files from the superseded state.
podman run --rm --pull=never \
  -v unflincher-data:/data:Z \
  -v "$RESTORE_TMP:/restore:ro,Z" \
  --entrypoint sh \
  localhost/unflincher:latest \
  -c "rm -f /data/unflincher.db-wal /data/unflincher.db-shm && cp /restore/unflincher.db /data/unflincher.db && sqlite3 /data/unflincher.db 'PRAGMA integrity_check; SELECT COUNT(*) FROM diary_entry;'"

# 5. Restart and require the running container to see the restored row count.
systemctl --user start unflincher.service
RUNNING_COUNT="$(
  podman exec unflincher sqlite3 /data/unflincher.db \
    "SELECT COUNT(*) FROM diary_entry;"
)"
test "$RUNNING_COUNT" = "$RESTORE_COUNT"
curl -fsS http://127.0.0.1:8096/healthz
```

The final `test` command is a hard gate. If it returns non-zero, leave the staged file in place and
recover from the verified pre-restore backup instead of continuing. After the service and
representative pages are verified, remove only the staged copy:

```bash
rm -f "$RESTORE_TMP/unflincher.db"
rmdir "$RESTORE_TMP"
```

## Active-prompt preservation manifest

Row counts alone do not prove the active Perspective survived a restore unchanged. Both
`verify-unflincher-backup.py --active-prompt-manifest` and the disposable restore drill
(`unflincher-restore-drill.sh`) additionally compare the currently active `persona_prompt` row's
identity manifest: `id`, `version_no`, a UTF-8 SHA-256 of `body_text`, `model`, `is_active`,
`created_at`, and `preset_key`. The manifest never includes or logs the body text itself, only its
hash, so an owner's private reflective instructions never appear in backup/restore output. The
restore drill fails loudly (and still cleans up its disposable resources) on any field mismatch;
it skips the comparison entirely only when the archive itself has no active prompt at all (e.g. a
legacy backup that predates `persona_prompt`).

```bash
python3 deploy/scripts/verify-unflincher-backup.py "$LATEST" --active-prompt-manifest
```
