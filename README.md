# Unflincher

A self-hosted AI journal for long-term reflection. Unflincher reads across years of your writing,
finds the patterns that repeat, backs each reading with evidence from your own entries, and lets you
argue with the interpretation instead of just accepting it.

Source available for noncommercial use.

![Life Report view showing a recurring pattern supported by dated evidence from a fictional sample journal.](site/assets/images/demo-report.png)

- Explore the demo: https://sinmentis.github.io/unflincher/demo/
- Install Unflincher: [docs/deployment.md](docs/deployment.md)

## What this is

A normal diary shows one day at a time, which hides the patterns that only surface across years.
Unflincher looks at the whole archive. It writes commentary on individual entries, builds a Life
Report that cites the specific entries behind each reading, and lets you talk back to that reading in
a conversation that pushes on your interpretation rather than simply agreeing with it.

- Single-user and self-hosted. No accounts and no multi-tenancy.
- FastAPI, Jinja2, htmx, and Server-Sent Events, with SQLite storage and no frontend build step.
- The GitHub Copilot SDK is the only model integration, reusing your existing Copilot access.
- The AI voice is not fixed. The Prompt Workshop lets you rewrite the persona and switch the model
  at any time.
- The interface chrome is translated into nine languages. Your entries, the persona prompt, and the
  generated text stay exactly as written.

## Product tour

- Timeline and entry reading: browse the archive by year and read any entry.
- Entry commentary: generate a mentor-style note on a single entry.
- Life Report: a cross-year reading that cites the entries it interprets.
- Entry and general conversations: challenge or extend a reading in dialogue.
- Prompt Workshop and version history: edit the persona, preview a change, and keep prior versions.

## Privacy and data flow

- Your diary entries, persona prompts, generated commentary, reports, and conversations are stored
  in your local SQLite database.
- Generation sends the selected persona prompt, the relevant diary context, and your current request
  through GitHub Copilot to the chosen model. Nothing else leaves your machine.
- The public demo contains only fictional data and performs no model calls, tracking, cookies,
  storage, or writable operations. Its buttons are inert.
- The public demo and landing page are hosted on GitHub Pages and are subject to GitHub's platform
  logging and privacy practices.

## Requirements and fast local trial

- Python 3.12 or newer.
- A GitHub Copilot subscription for the AI features.
- Optional Cloudflare account for the Access login gate on an internet-reachable deployment.

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
UNFLINCHER_REQUIRE_ACCESS_AUTH=false .venv/bin/uvicorn unflincher.app:app --reload
.venv/bin/pytest -q
```

Open http://localhost:8000. Browsing, writing, and reading work immediately. Generating AI
commentary, reports, or chat additionally requires `COPILOT_GITHUB_TOKEN` to be set. See
[docs/local-development.md](docs/local-development.md) for the full setup.

## Documentation

- Local development: [docs/local-development.md](docs/local-development.md)
- Installation and deployment behind Cloudflare Access: [docs/deployment.md](docs/deployment.md)
- Backups and restore: [docs/backup-and-restore.md](docs/backup-and-restore.md)
- Configuration reference: [docs/configuration.md](docs/configuration.md)

## Contributing, security, and support

- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md)
- Security policy: [SECURITY.md](SECURITY.md)
- Source code: https://github.com/sinmentis/unflincher
- Support and issues: https://github.com/sinmentis/unflincher/issues

## License

[PolyForm Noncommercial License 1.0.0](LICENSE). Source available for noncommercial use. You can
run, fork, and modify it for personal, hobby, educational, charitable, or government use. Commercial
use requires a separate license. For licensing questions, open a discussion on the repository.
