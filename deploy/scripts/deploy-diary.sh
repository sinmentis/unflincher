#!/bin/bash
# deploy/scripts/deploy-diary.sh
# Repeatable deploy: build -> assert the shared Copilot token secret exists -> restart -> healthcheck.
#
# This deliberately does NOT re-copy deploy/quadlet/*.container into
# ~/.config/containers/systemd/ on every run. That template (this repo is public/
# shareable) only has placeholder values (DIARY_CF_ACCESS_AUD, DIARY_OPERATOR_EMAIL,
# DIARY_CF_TEAM_DOMAIN, the domain in the Description= line) -- the real, filled-in
# values live ONLY in the already-deployed ~/.config/containers/systemd/diary.container,
# never committed to the repo. Overwriting it here on every code deploy would silently
# replace those real values with the template's placeholders and break Cloudflare Access
# auth on the next restart. If you actually change the .container/.volume unit files
# themselves (not just application code), copy and edit them into
# ~/.config/containers/systemd/ by hand, once, deliberately -- not via this script.
set -euo pipefail
cd "$(dirname "$0")/../.."

podman build -t localhost/diary:latest .

if ! podman secret exists unflincher-copilot-github-token 2>/dev/null; then
  echo "Missing shared secret 'unflincher-copilot-github-token'. Create it first:"
  # shellcheck disable=SC2016  # literal help text; $COPILOT_GITHUB_TOKEN must not expand here
  echo '  podman secret create unflincher-copilot-github-token <(printf "%s" "$COPILOT_GITHUB_TOKEN")'
  exit 1
fi

systemctl --user restart diary.service

sleep 2
curl -fsS http://127.0.0.1:8096/healthz
echo
echo "diary.service deployed and healthy."
