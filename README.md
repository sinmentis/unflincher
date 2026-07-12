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
`deploy/scripts/import-diary.sh /path/to/your-export.xlsx` — do this **before** starting the
service for the first time. If you're not migrating from Douban, skip this section entirely —
new entries can always be typed directly into the app's "写新日记" ("New Entry") page.

## Deployment (Podman + Quadlet + Cloudflare Access)

1. `podman build -t localhost/diary:latest .`
2. Create the podman secret containing a GitHub personal access token with Copilot SDK
   access (see "Getting a Copilot token" above). The default name expected by
   `deploy/quadlet/diary.container` and `deploy/scripts/deploy-diary.sh` is
   `diary-copilot-github-token`:
   `podman secret create diary-copilot-github-token <(printf "%s" "$YOUR_GITHUB_TOKEN")`
   If you rename it, update BOTH `diary.container`'s `Secret=` line AND set
   `DIARY_COPILOT_SECRET=your-name` when running `deploy/scripts/deploy-diary.sh` — the two
   must stay in sync or repeat deploys will fail with "Missing shared secret".
3. `deploy/scripts/import-diary.sh /path/to/your-export.xlsx` (if migrating existing entries).
4. Edit `deploy/quadlet/diary.container`'s placeholder values (`your-email@example.com`,
   `diary.yourdomain.com`, `your-team-name`, `REPLACE_WITH_YOUR_ACCESS_APP_AUD` — the last one
   comes from step 6 below), then:
   `cp deploy/quadlet/diary-data.volume deploy/quadlet/diary.container ~/.config/containers/systemd/`
   `systemctl --user daemon-reload && systemctl --user start diary.service`
5. Merge `deploy/cloudflared/diary-ingress.snippet.yml` into your own `~/.cloudflared/config.yml`,
   then `cloudflared tunnel route dns <your-tunnel-name> diary.yourdomain.com && systemctl --user
   restart cloudflared`. (This assumes you already have a named Cloudflare Tunnel set up — see
   [Cloudflare's tunnel docs](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/)
   if not.)
6. `CF_ACCOUNT_ID=... DIARY_DOMAIN=diary.yourdomain.com DIARY_OPERATOR_EMAIL=you@example.com
   CF_TOKEN=... ./deploy/create-access-diary-app.sh` — prints the AUD to put into step 4's
   `diary.container`. `CF_ACCOUNT_ID` is the 32-char hex ID shown on your Cloudflare dashboard's
   account home page (also in the URL after `/accounts/`); `CF_TOKEN` needs an API token with
   `Account.Access: Apps and Policies — Edit` permission.
7. Install the backup script once to a fixed, repo-checkout-independent location, then enable the
   timer: `mkdir -p ~/.local/bin && cp deploy/scripts/diary-backup.sh ~/.local/bin/ && chmod +x
   ~/.local/bin/diary-backup.sh && cp deploy/systemd/diary-backup.* ~/.config/systemd/user/ &&
   systemctl --user daemon-reload && systemctl --user enable --now diary-backup.timer`

If you troubleshoot a failed start: `systemctl --user status diary.service` and
`journalctl --user -u diary.service` show the container's logs and exit status.

## Repeat deploys

`deploy/scripts/deploy-diary.sh` — rebuilds the image and restarts the service. It deliberately
does **not** re-copy `deploy/quadlet/*.container` into `~/.config/containers/systemd/`, since that
template only holds placeholder values and blindly overwriting your already-deployed, filled-in
unit file would silently replace your real Cloudflare Access settings. If you change the unit
files themselves (not just application code), re-copy and re-edit them into
`~/.config/containers/systemd/` by hand, then `systemctl --user daemon-reload`.

## Backups & recovery

`deploy/scripts/diary-backup.sh` (installed to `~/.local/bin/`, run nightly by
`diary-backup.timer`) takes a WAL-safe SQLite online backup — it uses `sqlite3 .backup`, never a
raw file copy, so it can't catch the database mid-write — and gzips it to `~/backups/diary/`
(override with `DIARY_BACKUP_DIR`) with `0600` permissions. Backups older than 30 days are
auto-pruned (override with `DIARY_BACKUP_RETENTION_DAYS`).

To restore from a backup:

```bash
systemctl --user stop diary.service
gunzip -c ~/backups/diary/diary-YYYYMMDD-HHMMSS.db.gz > /tmp/diary-restore.db
podman volume mount diary-data   # or: run a throwaway container with the volume mounted
cp /tmp/diary-restore.db <volume-mountpoint>/diary.db
systemctl --user start diary.service
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
[Discussion](https://github.com/sinmentis/diary/discussions) on this repository.
