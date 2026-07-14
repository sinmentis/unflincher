# Deployment

Unflincher is self-hosted and source-available. This guide covers a rootless Podman plus Quadlet
deployment kept private behind a Cloudflare Tunnel and Cloudflare Access. The container publishes to
loopback only and has no login of its own, so Access is the only thing between the internet and your
diary. You can adapt the steps for Docker; the application has no Podman-specific code.

This guide changes only your own host. It does not touch any existing server and never rotates a
secret you already created.

## Requirements

- Python 3.12 or newer.
- Rootless Podman (the provided `Containerfile` and Quadlet units assume it).
- An active GitHub Copilot subscription for the model calls.
- A named Cloudflare Tunnel and a Cloudflare account, for a private internet-reachable deployment.

## GitHub Copilot authentication

Every AI feature (commentary, reports, chat) calls the model through the GitHub Copilot SDK.

- The SDK runtime auto-detects `COPILOT_GITHUB_TOKEN`, which is the highest-priority source it
  checks. Generation runs through a Copilot CLI runtime that downloads on first use; the image
  bakes it at build time with `python -m copilot download-runtime`.
- Use a fine-grained personal access token owned by your personal GitHub account (not an
  organization) with the account-level `Copilot Requests` permission. No repository permissions are
  required. Classic `ghp_` tokens are not supported, and an active Copilot seat is required.
- `gh auth token` is only a lower-priority local fallback for development. A headless production
  host should provide the token through the Podman secret that maps to `COPILOT_GITHUB_TOKEN`.
- Never put the token in a command argument or commit it anywhere. Create the secret from an
  interactive hidden read (step 2).

## Steps

1. Build the image:

   ```bash
   podman build -t localhost/unflincher:latest .
   ```

2. Create the Podman secret that holds the token. The default name expected by
   `deploy/quadlet/unflincher.container` and `deploy/scripts/deploy-unflincher.sh` is
   `unflincher-copilot-github-token`, and the deployed unit maps it to `COPILOT_GITHUB_TOKEN`:

   ```bash
   read -rs COPILOT_GITHUB_TOKEN
   printf '%s' "$COPILOT_GITHUB_TOKEN" | podman secret create unflincher-copilot-github-token -
   unset COPILOT_GITHUB_TOKEN
   ```

   Existing deployments do not need to recreate a write-only secret under a new name. Keep the
   current `Secret=` value in the deployed unit and set `UNFLINCHER_COPILOT_SECRET=your-name` when
   running `deploy/scripts/deploy-unflincher.sh` so the two stay in sync.

3. If migrating existing entries, run the importer before the first start:
   `deploy/scripts/import-unflincher.sh /path/to/your-export.xlsx`. See
   [import.md](import.md) for what the importer expects.

4. Fill in the placeholder values in `deploy/quadlet/unflincher.container`, then copy the units and
   start the service:

   ```bash
   cp deploy/quadlet/unflincher-data.volume deploy/quadlet/unflincher.container \
     ~/.config/containers/systemd/
   systemctl --user daemon-reload
   systemctl --user start unflincher.service
   ```

5. Merge `deploy/cloudflared/unflincher-ingress.snippet.yml` into your `~/.cloudflared/config.yml`,
   above the final catch-all, then route the hostname to the tunnel and restart it:

   ```bash
   cloudflared tunnel route dns <your-tunnel-name> unflincher.yourdomain.com
   systemctl --user restart cloudflared
   ```

   This assumes you already have a named Cloudflare Tunnel.

6. Create the Access application that gates the hostname behind an email login:

   ```bash
   CF_ACCOUNT_ID=... UNFLINCHER_DOMAIN=unflincher.yourdomain.com \
     UNFLINCHER_OPERATOR_EMAIL=you@example.com CF_TOKEN=... \
     ./deploy/create-access-unflincher-app.sh
   ```

   It prints the Access application audience tag to place into the unit's `UNFLINCHER_CF_ACCESS_AUD`
   from step 4. `CF_ACCOUNT_ID` is the account ID shown in the Cloudflare dashboard. `CF_TOKEN` needs
   an API token with the `Account.Access: Apps and Policies` permission set to Edit.

7. Install the backup scripts and enable the nightly timer. See
   [backup-and-recovery.md](backup-and-recovery.md).

Private by design: `deploy/quadlet/unflincher.container` publishes to `127.0.0.1:8096` only, and the
tunnel maps `unflincher.yourdomain.com` to that loopback port. The hostname must always stay paired
with the Access application, because the app itself has no login.

## Repeat deploys

`deploy/scripts/deploy-unflincher.sh` rebuilds the image and restarts the service. It does not
re-copy the Quadlet unit files, so your filled-in Cloudflare Access settings are never overwritten.
If you change the unit files, re-copy and re-edit them into `~/.config/containers/systemd/` by hand,
then run `systemctl --user daemon-reload`. The live service unit is `unflincher.service`.

Troubleshoot a failed start with `systemctl --user status unflincher.service` and
`journalctl --user -u unflincher.service`.

## Configuration

Every runtime setting is an environment variable. See [configuration.md](configuration.md) for the
full reference.
