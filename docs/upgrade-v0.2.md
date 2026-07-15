# Upgrade from v0.1 to v0.2

This is the required procedure for an existing v0.1 database. Ordinary v0.2 application startup
refuses to migrate a v0.1 database.

## Before the upgrade

1. Confirm the v0.1 service is healthy and the nightly backup timer is active.
2. Check out the exact v0.2 release commit with a completely clean working tree.
3. Keep the current deployed Quadlet file in place. It contains private values and must not be
   replaced by the repository template.
4. Make sure the existing Podman secret name matches `UNFLINCHER_COPILOT_SECRET`.

Build the exact target image:

```bash
podman pull docker.io/library/python:3.12-slim
REVISION="$(git rev-parse HEAD)"
VERSION="0.2.0"
UNFLINCHER_RELEASE_REVISION="$REVISION" \
UNFLINCHER_RELEASE_VERSION="$VERSION" \
  deploy/scripts/build-unflincher-release.sh
```

## Run the first upgrade

```bash
UNFLINCHER_RELEASE_IMAGE="localhost/unflincher:$REVISION" \
UNFLINCHER_EXPECTED_REVISION="$REVISION" \
UNFLINCHER_EXPECTED_VERSION="$VERSION" \
UNFLINCHER_DEPLOY_MODE=first-upgrade \
UNFLINCHER_COPILOT_SECRET=unflincher-copilot-github-token \
  deploy/scripts/deploy-unflincher.sh
```

The script performs these steps in order:

1. Verifies the local target image ID, OCI revision, OCI version, and matching runtime identity.
2. Records the running image and the effective `unflincher.service` contents and checksum in a
   private state directory.
3. Stops v0.1, then checks the stopped database for a running regeneration job.
4. Creates a pristine offline SQLite backup before any v0.2 migration.
5. Verifies the backup, active prompt manifest, and entry count.
6. Preserves the exact running v0.1 image under a rollback tag.
7. Runs a disposable restore drill with the rollback image.
8. Runs a second disposable restore drill that applies the v0.2 offline bootstrap only to the
   disposable copy.
9. Runs the same offline bootstrap against the production volume. The bootstrap validates the
   released schema, prompt identities, current-result compatibility, and running-job state before
   migration, then leaves maintenance locked.
10. Starts the exact target image and verifies health identity, database state, prompt and entry
    preservation, crawler defenses, and the local synthetic model probe.
11. Unlocks generation only after every check passes.

The script never publishes a release, changes GitHub settings, deploys GitHub Pages, or edits the
live Quadlet.

## Success evidence

The final output names the deployed revision and a run directory under:

```text
~/.local/state/unflincher-deploy/
```

The run directory contains target and prior image IDs, revisions, versions, the rollback tag,
pristine backup path, prompt and row-count manifests, locked and unlocked health responses, unit
checksums, and final status.

Verify the live endpoint:

```bash
curl -fsS http://127.0.0.1:8096/healthz
```

The response must report the expected revision and version with `generation_locked` set to `false`.

## Failure before live bootstrap

If backup creation or either disposable drill fails, the production database is unchanged. The
script restarts the prior service and exits non-zero.

## Failure after live bootstrap begins

The script stops production, attempts to reapply the maintenance lock, verifies the lock result, and
does not start v0.1 against the evolved database. If the lock cannot be confirmed, it records
`failed-unlocked`, prints a critical warning, and still keeps the service stopped. It also prints:

- the verified pristine backup path;
- the verified v0.1 rollback image tag;
- exact destructive restore commands.

Those commands replace the production database with the pre-upgrade backup and discard every later
change. They are a manual approval boundary. Do not run them until that data loss is explicitly
accepted. The disposable rollback drill has already proved the saved v0.1 image and backup can start
together on an alternate loopback port.

## After generation is unlocked

Do not start v0.1 against the evolved live database. A later rollback must use v0.2-compatible code,
or pair v0.1 with the verified pre-upgrade backup through the destructive restore procedure in
[backup-and-recovery.md](backup-and-recovery.md).

Publish `v0.2.0` only after production, the public site, and repository metadata all match the same
recorded release commit.
