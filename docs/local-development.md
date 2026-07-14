# Local development

Unflincher is a Python 3.12 FastAPI application. It uses Jinja2, htmx, and Server-Sent Events for
the UI, SQLite for storage, and has no frontend build step. Everything below runs on your own
machine and needs no container.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Run the app

```bash
UNFLINCHER_REQUIRE_ACCESS_AUTH=false .venv/bin/uvicorn unflincher.app:app --reload
```

Open http://localhost:8000. With `UNFLINCHER_REQUIRE_ACCESS_AUTH=false` there is no login gate,
which is intended for local, single-machine use only. Browsing, reading, and writing entries work
right away. Generating AI commentary, reports, or chat additionally requires `COPILOT_GITHUB_TOKEN`
in the environment (see below).

## GitHub Copilot for the AI features

Every AI feature talks to the model only through the GitHub Copilot SDK, not a generic provider API.

- Set `COPILOT_GITHUB_TOKEN` to a fine-grained personal access token owned by your personal GitHub
  account (not an organization) with the account-level `Copilot Requests` permission. No repository
  permissions are needed. Classic `ghp_` tokens are not supported, and an active Copilot seat is
  required.
- The SDK uses a Copilot CLI runtime. It downloads on first use, or you can fetch it once
  explicitly with `python -m copilot download-runtime`.
- If you are already signed in with the GitHub CLI, `gh auth token` works as a lower-priority local
  fallback. `COPILOT_GITHUB_TOKEN` always takes priority when it is set.

## Tests

```bash
.venv/bin/pytest -q
```

## Loading your own entries (optional)

If you are migrating from a Tofu browser-extension Excel export of a Douban diary, import it
straight into a local database:

```bash
.venv/bin/python -m unflincher.cli import --excel /path/to/your-export.xlsx --db unflincher.dev.db
```

The importer recognizes the extension's original sheet and column layout, so you do not need to
rename anything in the workbook. If you are not migrating, type entries directly on the New Entry
page in the app.

## Project layout

- `src/unflincher`: FastAPI application, routes, templates, LLM orchestration, and settings.
- `tests`: the pytest suite.
- `deploy`: the Containerfile image, Quadlet units, and the Cloudflare and backup scripts.
- `site`: the static demo and landing page published to GitHub Pages.

For production setup, see [deployment.md](deployment.md), [backup-and-restore.md](backup-and-restore.md),
and [configuration.md](configuration.md).
