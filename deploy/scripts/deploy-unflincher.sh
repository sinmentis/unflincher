#!/bin/bash
# deploy/scripts/deploy-unflincher.sh
# Repeatable deploy: build -> assert the shared Copilot token secret exists -> restart -> healthcheck.
#
# This deliberately does NOT re-copy deploy/quadlet/*.container into
# ~/.config/containers/systemd/ on every run. That template (this repo is public/
# shareable) only has placeholder values (UNFLINCHER_CF_ACCESS_AUD, UNFLINCHER_OPERATOR_EMAIL,
# UNFLINCHER_CF_TEAM_DOMAIN, the domain in the Description= line) -- the real, filled-in
# values live ONLY in the already-deployed ~/.config/containers/systemd/unflincher.container,
# never committed to the repo. Overwriting it here on every code deploy would silently
# replace those real values with the template's placeholders and break Cloudflare Access
# auth on the next restart. If you actually change the .container/.volume unit files
# themselves (not just application code), copy and edit them into
# ~/.config/containers/systemd/ by hand, once, deliberately -- not via this script.
#
# UNFLINCHER_COPILOT_SECRET lets you rename the shared podman secret without editing this
# script -- just make sure it matches the Secret= line in your deployed unflincher.container.
# NOTE: the default value below is deliberately still "diary-copilot-github-token" -- this
# project was renamed from "diary" to "unflincher", but that one secret's name was
# intentionally kept unchanged (see unflincher.container's own comment for why).
set -euo pipefail
cd "$(dirname "$0")/../.."

SECRET_NAME="${UNFLINCHER_COPILOT_SECRET:-diary-copilot-github-token}"

podman build -t localhost/unflincher:latest .

if ! podman secret exists "$SECRET_NAME" 2>/dev/null; then
  echo "Missing shared secret '$SECRET_NAME'. Create it first:"
  # shellcheck disable=SC2016  # literal help text; $COPILOT_GITHUB_TOKEN must not expand here
  echo "  podman secret create $SECRET_NAME <(printf \"%s\" \"\$COPILOT_GITHUB_TOKEN\")"
  exit 1
fi

systemctl --user restart unflincher.service

sleep 2
curl -fsS http://127.0.0.1:8096/healthz
echo
echo "unflincher.service deployed and healthy."
