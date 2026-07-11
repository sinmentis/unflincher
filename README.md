<!-- README.md -->
# diary — AI-annotated private diary

[![License: PolyForm Noncommercial](https://img.shields.io/badge/license-PolyForm%20Noncommercial-blue)](LICENSE)

A self-hosted, single-user web app that reads your own diary/journal export and gives you
LLM-generated "life mentor" commentary on it — via the [GitHub Copilot
SDK](https://github.com/github/copilot-sdk), not a fixed third-party API. The commentary's
persona and tone are fully yours to edit (see the Prompt Workshop page), not a one-size-fits-all
voice baked into the product.

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
  it purely on localhost with `DIARY_REQUIRE_ACCESS_AUTH=false` skips this entirely for local-only
  use.

## Requirements

- Python 3.12+
- [Podman](https://podman.io/) (the provided `Containerfile` + Quadlet units assume rootless
  Podman; adapt for Docker if you prefer — the app itself has no Podman-specific code)
- A GitHub Copilot subscription (or a Copilot-SDK-compatible BYOK setup) for the LLM calls
- (Optional) A Cloudflare account, only if you want the Cloudflare Access login gate for a
  publicly-reachable deployment

## Quick start (local dev)

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
DIARY_REQUIRE_ACCESS_AUTH=false .venv/bin/uvicorn diary.app:app --reload
.venv/bin/pytest
```

Open `http://localhost:8000`. With `DIARY_REQUIRE_ACCESS_AUTH=false`, there's no login wall at
all — this is for local, single-machine use only.

## Importing existing entries

If you're migrating from Douban's diary export format (`.xlsx`, with 标题/链接/创建时间/修改时间/
内容 columns): `deploy/scripts/import-diary.sh /path/to/your-export.xlsx` — do this **before**
starting the service for the first time. New entries going forward can be typed directly into the
app's "写新日记" page; the import script is only needed for a one-time bulk migration.

## Deployment (Podman + Quadlet + Cloudflare Access)

1. `podman build -t localhost/diary:latest .`
2. Create the shared `unflincher-copilot-github-token`-equivalent podman secret (name it whatever you
   like, then update `deploy/quadlet/diary.container`'s `Secret=` line to match) containing a
   GitHub personal access token with Copilot SDK access:
   `podman secret create your-copilot-token-name <(printf "%s" "$YOUR_GITHUB_TOKEN")`
3. `deploy/scripts/import-diary.sh /path/to/your-export.xlsx` (if migrating existing entries).
4. Edit `deploy/quadlet/diary.container`'s placeholder values (`your-email@example.com`,
   `diary.yourdomain.com`, `REPLACE_WITH_YOUR_ACCESS_APP_AUD` — the last one comes from step 6
   below), then:
   `cp deploy/quadlet/diary-data.volume deploy/quadlet/diary.container ~/.config/containers/systemd/`
   `systemctl --user daemon-reload && systemctl --user start diary.service`
5. Merge `deploy/cloudflared/diary-ingress.snippet.yml` into your own `~/.cloudflared/config.yml`,
   then `cloudflared tunnel route dns <your-tunnel-name> diary.yourdomain.com && systemctl --user
   restart cloudflared`.
6. `CF_ACCOUNT_ID=... DIARY_DOMAIN=diary.yourdomain.com DIARY_OPERATOR_EMAIL=you@example.com
   CF_TOKEN=... ./deploy/create-access-diary-app.sh` — prints the AUD to put into step 4's
   `diary.container`.
7. `cp deploy/systemd/diary-backup.* ~/.config/systemd/user/ && systemctl --user daemon-reload &&
   systemctl --user enable --now diary-backup.timer`

## Repeat deploys

`deploy/scripts/deploy-diary.sh`

## Configuration reference

All settings are environment variables, read by `src/diary/config.py`:

| Variable | Default | Description |
|---|---|---|
| `DIARY_DB` | `diary.dev.db` | SQLite database file path |
| `DIARY_LLM_MODEL` | `claude-sonnet-4.6` | Default model for the active persona |
| `DIARY_BATCH_CONCURRENCY` | `3` | Max concurrent items when regenerating all commentary |
| `DIARY_LLM_CONCURRENCY` | `4` | Max concurrent LLM sessions on the shared Copilot client |
| `DIARY_CF_TEAM_DOMAIN` | *(empty)* | Cloudflare Access team name (short form, no `.cloudflareaccess.com` suffix) |
| `DIARY_CF_ACCESS_AUD` | *(empty)* | Cloudflare Access application audience tag |
| `DIARY_OPERATOR_EMAIL` | *(empty)* | The email allowed to authenticate via Cloudflare Access |
| `DIARY_REQUIRE_ACCESS_AUTH` | `true` | Set to `false` to disable the Cloudflare Access login check entirely (local dev only) |

## License

[PolyForm Noncommercial License 1.0.0](LICENSE) — free to use, modify, and share for any
noncommercial purpose. Commercial use requires contacting the author.
