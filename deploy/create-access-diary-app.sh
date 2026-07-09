#!/bin/bash
# deploy/create-access-diary-app.sh
# Creates the Cloudflare Access application that gates diary.yourdomain.com behind the owner's
# email OTP. Mirrors the existing dash/ssh Access app scripts in shunlyu-infra — same
# account/token-loading convention. Requires: CF_TOKEN env var (or ~/.cloudflared/cf_token),
# jq, curl. Prints the app's AUD tag at the end — put it into diary.container's
# DIARY_CF_ACCESS_AUD before starting the service (Task 19 step 4).
set -euo pipefail

source "$HOME/.config/diary/infra.env"
DIARY_OPERATOR_EMAIL="${DIARY_OPERATOR_EMAIL:-$OPERATOR_EMAIL}"
TOKEN="${CF_TOKEN:-$(cat ~/.cloudflared/cf_token 2>/dev/null)}"
: "${TOKEN:?set CF_TOKEN or ~/.cloudflared/cf_token to a scoped Cloudflare API token}"
API="https://api.cloudflare.com/client/v4/accounts/${CF_ACCOUNT_ID}/access"

APP_JSON=$(curl -fsS -X POST "$API/apps" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"diary.yourdomain.com","domain":"diary.yourdomain.com","type":"self_hosted",
       "session_duration":"24h","auto_redirect_to_identity":false}')
APP_ID=$(echo "$APP_JSON" | jq -r '.result.id')
APP_AUD=$(echo "$APP_JSON" | jq -r '.result.aud')

curl -fsS -X POST "$API/apps/${APP_ID}/policies" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"name\":\"Allow ${DIARY_OPERATOR_EMAIL} via OTP\",\"decision\":\"allow\",\"precedence\":1,
       \"include\":[{\"email\":{\"email\":\"${DIARY_OPERATOR_EMAIL}\"}}]}" | jq '{policy_id:.result.id}'

echo "app_id=${APP_ID}"
echo "aud=${APP_AUD}   <- put this into diary.container's DIARY_CF_ACCESS_AUD"
