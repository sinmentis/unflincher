# Configuration reference

Every setting is an environment variable. The application settings are read by
`src/unflincher/config.py`. Copilot authentication and the deployment secret name are handled by the
GitHub Copilot SDK and the deploy tooling.

## Application settings

The defaults below are the local development defaults from `src/unflincher/config.py`. Production
values are injected by the Quadlet unit's `Environment=` and `Secret=` directives (see
`deploy/quadlet/unflincher.container`).

| Variable | Default | Description |
|---|---|---|
| `UNFLINCHER_DB` | `unflincher.dev.db` | SQLite database file path |
| `UNFLINCHER_LLM_MODEL` | `claude-sonnet-4.6` | Default model for the active persona |
| `UNFLINCHER_BATCH_CONCURRENCY` | `3` | Max concurrent items when regenerating all commentary |
| `UNFLINCHER_LLM_CONCURRENCY` | `4` | Max concurrent model sessions on the shared Copilot client |
| `UNFLINCHER_CF_TEAM_DOMAIN` | empty | Cloudflare Access team name, short form without the `.cloudflareaccess.com` suffix |
| `UNFLINCHER_CF_ACCESS_AUD` | empty | Cloudflare Access application audience tag |
| `UNFLINCHER_OPERATOR_EMAIL` | empty | The email allowed to authenticate through Cloudflare Access |
| `UNFLINCHER_REQUIRE_ACCESS_AUTH` | `true` | Set to `false` to disable the Cloudflare Access login check (local dev only) |

`UNFLINCHER_LLM_MODEL` and `UNFLINCHER_LLM_CONCURRENCY` are also read in `src/unflincher/llm.py`,
which drives every generation path through one shared Copilot client and bounds how many model
sessions run on it at once.

## Copilot authentication

- `COPILOT_GITHUB_TOKEN` holds the GitHub token used for all model calls. The Copilot SDK runtime
  auto-detects it, so the application never reads it directly. See
  [deployment.md](deployment.md) for how to obtain and store it.
- `UNFLINCHER_COPILOT_SECRET` is read only by `deploy/scripts/deploy-unflincher.sh`. It names the
  Podman secret the script checks for and defaults to `unflincher-copilot-github-token`. Set it when
  an existing install keeps a differently named secret. The deployed unit maps that secret to
  `COPILOT_GITHUB_TOKEN`.

## Backup and restore variables

The backup and restore scripts read their own environment variables, such as the backup directory,
retention window, and restore port. Those are documented in
[backup-and-recovery.md](backup-and-recovery.md).
