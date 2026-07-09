<!-- README.md -->
# diary — AI-annotated private diary

Private, single-user web app. See the design docs in `shunlyu-infra/docs/superpowers/specs/`
(`2026-07-09-diary-ai-mentor-design.md`, `2026-07-09-diary-technical-design.md`) and the
implementation plan in `shunlyu-infra/docs/superpowers/plans/2026-07-09-diary-implementation.md`.

## First-time deploy (in order)

1. `podman build -t localhost/diary:latest .`
2. `podman secret create diary-llm-key <(printf '%s' "$ANTHROPIC_API_KEY")`
3. `deploy/scripts/import-diary.sh /path/to/豆伴export.xlsx` — **before** starting the service.
4. Edit `deploy/quadlet/diary.container`'s three `REPLACE_WITH_*` values (see Task 20 for how
   to obtain them), then:
   `cp deploy/quadlet/diary-data.volume deploy/quadlet/diary.container ~/.config/containers/systemd/`
   `systemctl --user daemon-reload && systemctl --user start diary.service`
5. Merge `deploy/cloudflared/diary-ingress.snippet.yml` into `~/.cloudflared/config.yml`,
   then `cloudflared tunnel route dns unflincher-host diary.yourdomain.com && systemctl --user restart cloudflared`.
6. `CF_TOKEN=... ./deploy/create-access-diary-app.sh` (account ID + operator email now come
   from `shunlyu-infra/deploy/infra.env`; override `DIARY_OPERATOR_EMAIL=...` if it should
   differ from the default operator)
7. `cp deploy/systemd/diary-backup.* ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now diary-backup.timer`

## Repeat deploys

`deploy/scripts/deploy-diary.sh`

## Local dev

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
DIARY_REQUIRE_ACCESS_AUTH=false .venv/bin/uvicorn diary.app:app --reload
.venv/bin/pytest
```
