<!-- README.md -->
# Unflincher (诤友) — AI-annotated private diary

[![License: PolyForm Noncommercial](https://img.shields.io/badge/license-PolyForm%20Noncommercial-blue)](LICENSE)

A self-hosted, single-user web app that reads your own diary/journal export and gives you
LLM-generated "life mentor" commentary on it — via the [GitHub Copilot
SDK](https://github.com/github/copilot-sdk), not a fixed third-party API. The commentary's
persona and tone are fully yours to edit (see the Prompt Workshop page), not a one-size-fits-all
voice baked into the product.

🌐 **UI available in 9 languages:** English · 简体中文 · 日本語 · 한국어 · Español · Français ·
Deutsch · Русский · Português (this only translates the app's own chrome — nav, buttons, status
messages; your diary entries, the AI's persona prompt, and its generated commentary always stay
exactly as written/generated, in whatever language that is).

## What this is

- **Single-user, self-hosted.** No accounts, no multi-tenancy — this is a personal tool you run
  for yourself, not a SaaS product.
- **FastAPI + Jinja2 + htmx + Server-Sent Events**, SQLite storage, no frontend build step.
- **The GitHub Copilot SDK** is the only LLM integration — reuses whatever Copilot subscription/
  authentication you already have (a GitHub personal access token), rather than requiring a
  separate paid API key for a different provider.
- **Fully editable AI persona** — the "life mentor" voice, and which model generates it, are
  both changeable from the Prompt Workshop page at any time, with a live preview before you
  commit to a change.
- **Optional Cloudflare Access gate** — if you deploy this somewhere internet-reachable, an
  email-OTP login wall (via Cloudflare Access) is the recommended way to keep it private; running
  it purely on localhost with `UNFLINCHER_REQUIRE_ACCESS_AUTH=false` skips this entirely for local-only
  use.

## Requirements

- Python 3.12+
- [Podman](https://podman.io/) (the provided `Containerfile` + Quadlet units assume rootless
  Podman; adapt for Docker if you prefer — the app itself has no Podman-specific code)
- A GitHub Copilot subscription (or a Copilot-SDK-compatible BYOK setup) for the LLM calls
- (Optional) A Cloudflare account, only if you want the Cloudflare Access login gate for a
  publicly-reachable deployment

### Getting a Copilot token

Every AI feature (commentary, reports, chat) requires an active **GitHub Copilot subscription**
— there is no "bring your own OpenAI/Anthropic key" option, since the app talks to the LLM
exclusively through the [GitHub Copilot SDK](https://github.com/github/copilot-sdk), not a
generic provider API.

1. Confirm you have Copilot access on your GitHub account (an Individual/Business/Enterprise
   Copilot seat).
2. Create a [fine-grained personal access
   token](https://github.com/settings/personal-access-tokens/new) with Copilot access enabled
   for your account. No repository permissions are required — this token is used purely to
   authenticate to the Copilot SDK's backend, not to access any repo.
3. Set it as `COPILOT_GITHUB_TOKEN` in your environment (local dev) or as the podman secret
   described in the deployment steps below (production). `CopilotClient()` (in
   `src/unflincher/llm.py`) auto-detects this env var — you don't pass it explicitly anywhere else.
4. To sanity-check the token works before wiring up the whole app: `COPILOT_GITHUB_TOKEN=... \
   python -c "from copilot import CopilotClient; import asyncio; asyncio.run(CopilotClient().start())"`
   should exit without an auth error.

## Quick start (local dev)

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
UNFLINCHER_REQUIRE_ACCESS_AUTH=false .venv/bin/uvicorn unflincher.app:app --reload
.venv/bin/pytest
```

Open `http://localhost:8000`. With `UNFLINCHER_REQUIRE_ACCESS_AUTH=false`, there's no login wall at
all — this is for local, single-machine use only. Browsing, writing, and reading entries works
immediately with no further setup; generating AI commentary/reports/chat additionally requires
`COPILOT_GITHUB_TOKEN` to be set (see "Getting a Copilot token" above).

## Importing existing entries

If you're migrating from a **豆伴 (Tofu) Chrome extension** export of your [Douban](https://www.douban.com/)
diary (Douban is a Chinese social/review/journaling platform; 豆伴 is a third-party browser
extension that exports its diary entries to Excel — this is not an official Douban export
feature): the exported `.xlsx` file has a sheet named 日记 with columns 标题 (title) / 链接 (link)
/ 创建时间 (created) / 修改时间 (modified) / 内容 (content). Run
`deploy/scripts/import-unflincher.sh /path/to/your-export.xlsx` — do this **before** starting the
service for the first time. If you're not migrating from Douban, skip this section entirely —
new entries can always be typed directly into the app's "写新日记" ("New Entry") page.

## Deployment (Podman + Quadlet + Cloudflare Access)

1. `podman build -t localhost/unflincher:latest .`
2. Create the podman secret containing a GitHub personal access token with Copilot SDK
   access (see "Getting a Copilot token" above). The default name expected by
   `deploy/quadlet/unflincher.container` and `deploy/scripts/deploy-unflincher.sh` is
   `diary-copilot-github-token` (this specific name is unchanged from before this project's
   rename — see that file's own comments for why):
   `podman secret create diary-copilot-github-token <(printf "%s" "$YOUR_GITHUB_TOKEN")`
   If you rename it, update BOTH `unflincher.container`'s `Secret=` line AND set
   `UNFLINCHER_COPILOT_SECRET=your-name` when running `deploy/scripts/deploy-unflincher.sh` —
   the two must stay in sync or repeat deploys will fail with "Missing shared secret".
3. `deploy/scripts/import-unflincher.sh /path/to/your-export.xlsx` (if migrating existing entries).
4. Edit `deploy/quadlet/unflincher.container`'s placeholder values (`your-email@example.com`,
   `unflincher.yourdomain.com`, `your-team-name`, `REPLACE_WITH_YOUR_ACCESS_APP_AUD` — the last
   one comes from step 6 below), then:
   `cp deploy/quadlet/unflincher-data.volume deploy/quadlet/unflincher.container ~/.config/containers/systemd/`
   `systemctl --user daemon-reload && systemctl --user start unflincher.service`
5. Merge `deploy/cloudflared/unflincher-ingress.snippet.yml` into your own
   `~/.cloudflared/config.yml`, then `cloudflared tunnel route dns <your-tunnel-name>
   unflincher.yourdomain.com && systemctl --user restart cloudflared`. (This assumes you already
   have a named Cloudflare Tunnel set up — see [Cloudflare's tunnel
   docs](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/)
   if not.)
6. `CF_ACCOUNT_ID=... UNFLINCHER_DOMAIN=unflincher.yourdomain.com
   UNFLINCHER_OPERATOR_EMAIL=you@example.com CF_TOKEN=... ./deploy/create-access-unflincher-app.sh`
   — prints the AUD to put into step 4's `unflincher.container`. `CF_ACCOUNT_ID` is the 32-char
   hex ID shown on your Cloudflare dashboard's account home page (also in the URL after
   `/accounts/`); `CF_TOKEN` needs an API token with `Account.Access: Apps and Policies — Edit`
   permission.
7. Install the backup and verifier scripts once to a fixed, repo-checkout-independent location,
   then enable the timer:

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

If you troubleshoot a failed start: `systemctl --user status unflincher.service` and
`journalctl --user -u unflincher.service` show the container's logs and exit status.

## Repeat deploys

`deploy/scripts/deploy-unflincher.sh` — rebuilds the image and restarts the service. It deliberately
does **not** re-copy `deploy/quadlet/*.container` into `~/.config/containers/systemd/`, since that
template only holds placeholder values and blindly overwriting your already-deployed, filled-in
unit file would silently replace your real Cloudflare Access settings. If you change the unit
files themselves (not just application code), re-copy and re-edit them into
`~/.config/containers/systemd/` by hand, then `systemctl --user daemon-reload`. The live service
unit is `unflincher.service`.

## Backups & recovery

`unflincher-backup.timer` runs `~/.local/bin/unflincher-backup.sh` nightly. The script uses
SQLite's online `.backup` command, so it is safe while the WAL-mode application is running. It
writes a hidden `.partial` gzip first, verifies that the archive decompresses, runs
`PRAGMA integrity_check`, and checks its entry count against the live count before atomically
publishing `unflincher-*.db.gz` with `0600` permissions. Failed checks leave no final-looking
artifact. Backups go to `~/backups/unflincher/` by default and are pruned after 30 days; override
these with `UNFLINCHER_BACKUP_DIR` and `UNFLINCHER_BACKUP_RETENTION_DAYS`.

These are still backups on the same host. A passing check or drill does not provide an off-host
copy.

### Test a backup without touching production

Run the disposable restore drill before relying on an artifact. It verifies all application-table
row counts, starts the real image against a uniquely named temporary volume on loopback port
`18096`, and checks health, timeline, report, chat, workshop, and one entry page. It never mounts
the production `unflincher-data` volume and removes its temporary container, volume, and database
on success or failure. Set `UNFLINCHER_RESTORE_PORT` if `18096` is occupied.

```bash
LATEST="$(ls -1t "$HOME"/backups/unflincher/unflincher-*.db.gz | head -1)"
EXPECTED="$(
  podman exec unflincher sqlite3 /data/unflincher.db \
    "SELECT COUNT(*) FROM diary_entry;"
)"
deploy/scripts/unflincher-restore-drill.sh "$LATEST" "$EXPECTED"
```

### Restore production

This procedure overwrites the production database. First run the disposable drill above, then take
one final verified backup of the current state. Keep the staged database until the restarted
service and representative pages have been checked. **Run the commands in one shell and stop
immediately if any command returns a non-zero status; do not continue to the next numbered step.**

```bash
# 1. Preserve and verify the current production state.
PRE_RESTORE_DIR="$HOME/backups/unflincher/pre-restore"
UNFLINCHER_BACKUP_DIR="$PRE_RESTORE_DIR" \
  ~/.local/bin/unflincher-backup.sh

# 2. Select and verify the artifact to restore. This example selects the latest normal backup.
RESTORE_ARCHIVE="$(
  ls -1t "$HOME"/backups/unflincher/unflincher-*.db.gz | head -1
)"
RESTORE_COUNT="$(
  python3 ~/.local/bin/verify-unflincher-backup.py "$RESTORE_ARCHIVE"
)"

# 3. Stop writes and stage the verified database.
systemctl --user stop unflincher.service
RESTORE_TMP="$(mktemp -d)"
gunzip -c "$RESTORE_ARCHIVE" > "$RESTORE_TMP/unflincher.db"

# 4. Replace the database and remove WAL/SHM files from the superseded database state.
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

The one-shot copy command must print `ok` and the expected row count. The final `test` command is a
hard gate: if it returns non-zero, leave the staged file in place and recover from the verified
pre-restore backup instead of continuing. After the service and representative pages are verified,
remove only the staged copy:

```bash
rm -f "$RESTORE_TMP/unflincher.db"
rmdir "$RESTORE_TMP"
```

## Configuration reference

All settings are environment variables, read by `src/unflincher/config.py`:

| Variable | Default | Description |
|---|---|---|
| `UNFLINCHER_DB` | `unflincher.dev.db` | SQLite database file path |
| `UNFLINCHER_LLM_MODEL` | `claude-sonnet-4.6` | Default model for the active persona |
| `UNFLINCHER_BATCH_CONCURRENCY` | `3` | Max concurrent items when regenerating all commentary |
| `UNFLINCHER_LLM_CONCURRENCY` | `4` | Max concurrent LLM sessions on the shared Copilot client |
| `UNFLINCHER_CF_TEAM_DOMAIN` | *(empty)* | Cloudflare Access team name (short form, no `.cloudflareaccess.com` suffix) |
| `UNFLINCHER_CF_ACCESS_AUD` | *(empty)* | Cloudflare Access application audience tag |
| `UNFLINCHER_OPERATOR_EMAIL` | *(empty)* | The email allowed to authenticate via Cloudflare Access |
| `UNFLINCHER_REQUIRE_ACCESS_AUTH` | `true` | Set to `false` to disable the Cloudflare Access login check entirely (local dev only) |

## License

[PolyForm Noncommercial License 1.0.0](LICENSE) — the full legal text always governs; the
summary below is plain-English orientation, not legal advice.

- ✅ You **can** run this for your own personal diary, fork it, modify it, and share your
  changes — for personal, hobby, educational, charitable, or government use.
- ❌ You **cannot** use it for or within a for-profit business (including running it
  internally at a company), or build a commercial product/service on top of it, without a
  separate commercial license.
- Not sure whether your use counts as noncommercial? Company-internal use generally does
  **not** qualify, even without reselling anything — when in doubt, ask first.

**Want a commercial license, or have a licensing question?** Open a
[Discussion](https://github.com/sinmentis/unflincher/discussions) on this repository.
