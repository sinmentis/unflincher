# Release notes: v0.1.0

Published: 2026-07-15.

Unflincher v0.1.0 is the first source-available release.

## Install

See the [deployment guide](deployment.md) for the self-hosted deployment behind Cloudflare Access,
and the README for a fast local trial.

## Privacy disclosure

- Diary entries, prompts, commentary, reports, and chat history are stored in your local SQLite
  database.
- Generation sends the selected persona prompt, the relevant diary context, and your current request
  through GitHub Copilot to the chosen model.
- The public demo contains only fictional data and never performs generation.
- The public site is hosted on GitHub Pages and is subject to GitHub's platform logging and privacy
  practices.

## Known constraints

- Single-user only. No accounts or multi-tenancy.
- Requires GitHub Copilot access. No other provider is supported in this release.
- Setup requires a host you control and basic terminal work.

## License

Source available for noncommercial use under the PolyForm Noncommercial License 1.0.0.
