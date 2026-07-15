# Deployment

Unflincher is self-hosted and source-available. This guide covers a rootless Podman plus Quadlet
deployment kept private behind a Cloudflare Tunnel and Cloudflare Access. The container publishes
to loopback only and has no login of its own, so Access is the boundary between the internet and
your journal.

## Requirements

- Python 3.12 or newer.
- Rootless Podman and systemd user services.
- An active GitHub Copilot subscription for model calls.
- A named Cloudflare Tunnel and a Cloudflare account for private internet access.

## Build an exact image

Release deployment never builds a mutable working tree. Check out the exact release commit, keep the
tree clean, make the base image available locally, and build one SHA-tagged image:

```bash
podman pull docker.io/library/python:3.12-slim
REVISION="$(git rev-parse HEAD)"
VERSION="0.2.0"
UNFLINCHER_RELEASE_REVISION="$REVISION" \
UNFLINCHER_RELEASE_VERSION="$VERSION" \
  deploy/scripts/build-unflincher-release.sh
```

The build script refuses a dirty tree or a revision that differs from `HEAD`. It passes the revision,
version, and commit timestamp into the OCI labels and runtime environment, tags only
`localhost/unflincher:$REVISION`, and never changes `latest`.

For a new installation, point the Quadlet tag at that image once:

```bash
podman tag "localhost/unflincher:$REVISION" localhost/unflincher:latest
```

## GitHub Copilot authentication

Every generation feature calls the model through the GitHub Copilot SDK. The image downloads the
Copilot CLI runtime at build time. Production supplies `COPILOT_GITHUB_TOKEN` through a Podman
secret.

Use a fine-grained personal access token owned by your personal GitHub account with the account-level
`Copilot Requests` permission. No repository permissions are required. Never put the token in a
command argument or commit it.

Create the default secret from an interactive hidden read:

```bash
read -rs COPILOT_GITHUB_TOKEN
printf '%s' "$COPILOT_GITHUB_TOKEN" \
  | podman secret create unflincher-copilot-github-token -
unset COPILOT_GITHUB_TOKEN
```

Existing deployments may keep a different secret name. Keep the deployed Quadlet `Secret=` value
unchanged and pass the same name as `UNFLINCHER_COPILOT_SECRET` during deployment.

## Install the service

If you are importing a Douban archive into a new installation, run
`deploy/scripts/import-unflincher.sh /path/to/your-export.xlsx` before the first start. See
[import.md](import.md).

Fill in the placeholders in `deploy/quadlet/unflincher.container`, then copy the units:

```bash
cp deploy/quadlet/unflincher-data.volume deploy/quadlet/unflincher.container \
  ~/.config/containers/systemd/
systemctl --user daemon-reload
systemctl --user start unflincher.service
```

The deployed unit contains private Cloudflare and operator values. Repeat deployments never overwrite
it. `deploy/scripts/deploy-unflincher.sh` keeps the unit's
`Image=localhost/unflincher:latest` line and moves only the local image tag.

Merge `deploy/cloudflared/unflincher-ingress.snippet.yml` into
`~/.cloudflared/config.yml` above the final catch-all, then route the hostname:

```bash
cloudflared tunnel route dns <your-tunnel-name> unflincher.yourdomain.com
systemctl --user restart cloudflared
```

Create the Cloudflare Access application:

```bash
CF_ACCOUNT_ID=... UNFLINCHER_DOMAIN=unflincher.yourdomain.com \
  UNFLINCHER_OPERATOR_EMAIL=you@example.com CF_TOKEN=... \
  ./deploy/create-access-unflincher-app.sh
```

`CF_TOKEN` needs `Account.Access: Apps and Policies` set to Edit. Put the printed audience tag into
the deployed unit's `UNFLINCHER_CF_ACCESS_AUD` value.

Install the backup scripts and nightly timer from
[backup-and-recovery.md](backup-and-recovery.md).

## Upgrade an existing v0.1 installation

Do not start a v0.2 image directly against a v0.1 database. Use the fail-locked procedure in
[upgrade-v0.2.md](upgrade-v0.2.md). It stops v0.1, verifies no regeneration is running, creates and
drills a pristine backup, runs the offline bootstrap, and keeps generation locked until every target
verification passes.

## Repeat v0.2 deployments

Build the exact image first, then run the deployment in explicit `routine` mode:

```bash
UNFLINCHER_RELEASE_IMAGE="localhost/unflincher:$REVISION" \
UNFLINCHER_EXPECTED_REVISION="$REVISION" \
UNFLINCHER_EXPECTED_VERSION="$VERSION" \
UNFLINCHER_DEPLOY_MODE=routine \
UNFLINCHER_COPILOT_SECRET=unflincher-copilot-github-token \
  deploy/scripts/deploy-unflincher.sh
```

The script locks generation, waits for active work to drain, stops the service, creates a verified
backup, preserves the running image under a rollback tag, drills both rollback and target images,
repoints `latest`, and starts the target. While still locked, it verifies:

- health revision, version, and generation-lock state;
- running container image ID and OCI revision;
- completed database bootstrap state with no leases or running jobs;
- active prompt identity and entry count preservation;
- private crawler headers and disallow-all `robots.txt`;
- the local synthetic, non-persisting model probe;
- unchanged effective systemd unit contents and checksum.

Only then does it run the confirmed maintenance unlock. Deployment state, image IDs, rollback tag,
backup path, health responses, and the private effective unit snapshot are stored with mode `0700`
under `~/.local/state/unflincher-deploy/` by default.

Troubleshoot a failed start with:

```bash
systemctl --user status unflincher.service
journalctl --user -u unflincher.service
```

## Configuration

See [configuration.md](configuration.md) for application and deployment variables.
