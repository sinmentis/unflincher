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
| `UNFLINCHER_LLM_MODEL` | `claude-sonnet-4.6` | Default model for the active Perspective |
| `UNFLINCHER_BATCH_CONCURRENCY` | `3` | Max concurrent items when regenerating all Entry Reflections |
| `UNFLINCHER_LLM_CONCURRENCY` | `4` | Max concurrent model sessions on the shared Copilot client |
| `UNFLINCHER_CF_TEAM_DOMAIN` | empty | Cloudflare Access team name, short form without the `.cloudflareaccess.com` suffix |
| `UNFLINCHER_CF_ACCESS_AUD` | empty | Cloudflare Access application audience tag |
| `UNFLINCHER_OPERATOR_EMAIL` | empty | The email allowed to authenticate through Cloudflare Access |
| `UNFLINCHER_REQUIRE_ACCESS_AUTH` | `true` | Set to `false` to disable the Cloudflare Access login check (local dev only) |
| `UNFLINCHER_REVISION` | `development` | Image revision returned by `/healthz`; release images bake the exact commit SHA |
| `UNFLINCHER_VERSION` | installed package version | Version returned by `/healthz`; release images bake the release version |

`UNFLINCHER_LLM_MODEL` and `UNFLINCHER_LLM_CONCURRENCY` are also read in `src/unflincher/llm.py`,
which drives every generation path through one shared Copilot client and bounds how many model
sessions run on it at once.

## Perspective behavior

Analyst is the default Perspective for a new database. Unflincher uses one globally active
Perspective for future Entry Reflections, Life Reports, and Conversations. Change it in Prompt
Workshop without restarting the application. Existing generated output keeps the prompt version that
created it.

A saved prompt is labeled Companion, Coach, Challenger, or Analyst only when its text exactly matches
a shipped preset. Any edited preset or user-authored instruction is stored and displayed as Custom.
Existing installations keep their active prompt unchanged during migration and display it as Custom.

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

## Release deployment variables

`deploy/scripts/build-unflincher-release.sh` requires `UNFLINCHER_RELEASE_REVISION` and
`UNFLINCHER_RELEASE_VERSION`. The deploy script requires:

| Variable | Description |
|---|---|
| `UNFLINCHER_RELEASE_IMAGE` | Prebuilt local SHA-tagged image to deploy |
| `UNFLINCHER_EXPECTED_REVISION` | Exact 40-character Git commit SHA expected in the image and health response |
| `UNFLINCHER_EXPECTED_VERSION` | Exact release version expected in the image and health response |
| `UNFLINCHER_DEPLOY_MODE` | Explicitly `first-upgrade` or `routine`; the script never guesses the version boundary |
| `UNFLINCHER_COPILOT_SECRET` | Podman secret name used by the deployed Quadlet |
| `UNFLINCHER_DEPLOY_STATE_DIR` | Private deployment evidence directory; defaults to `~/.local/state/unflincher-deploy` |

See [deployment.md](deployment.md) and [upgrade-v0.2.md](upgrade-v0.2.md).
