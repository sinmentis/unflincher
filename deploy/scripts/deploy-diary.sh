#!/bin/bash
# deploy/scripts/deploy-diary.sh
# Repeatable deploy: build -> assert the LLM secret exists -> reload -> restart -> healthcheck.
set -euo pipefail
cd "$(dirname "$0")/../.."

podman build -t localhost/diary:latest .

if ! podman secret exists diary-llm-key 2>/dev/null; then
  echo "Missing secret 'diary-llm-key'. Create it first:"
  # shellcheck disable=SC2016  # literal help text; $ANTHROPIC_API_KEY must not expand here
  echo '  podman secret create diary-llm-key <(printf "%s" "$ANTHROPIC_API_KEY")'
  exit 1
fi

cp deploy/quadlet/diary-data.volume deploy/quadlet/diary.container ~/.config/containers/systemd/
systemctl --user daemon-reload
systemctl --user restart diary.service

sleep 2
curl -fsS http://127.0.0.1:8096/healthz
echo
echo "diary.service deployed and healthy."
