#!/bin/bash
# deploy/create-access-unflincher-app.sh
# Creates the Cloudflare Access application that gates your unflincher deployment behind an
# email OTP login. Requires: CF_TOKEN env var (or ~/.cloudflared/cf_token), CF_ACCOUNT_ID env
# var, UNFLINCHER_DOMAIN env var (e.g. unflincher.yourdomain.com), UNFLINCHER_OPERATOR_EMAIL
# env var, jq, curl. Prints the app's AUD tag at the end -- put it into unflincher.container's
# UNFLINCHER_CF_ACCESS_AUD before starting the service.
set -euo pipefail

: "${CF_ACCOUNT_ID:?set CF_ACCOUNT_ID to your Cloudflare account ID}"
: "${UNFLINCHER_DOMAIN:?set UNFLINCHER_DOMAIN to the hostname you want unflincher served on, e.g. unflincher.yourdomain.com}"
: "${UNFLINCHER_OPERATOR_EMAIL:?set UNFLINCHER_OPERATOR_EMAIL to the email allowed to log in}"
TOKEN="${CF_TOKEN:-$(cat ~/.cloudflared/cf_token 2>/dev/null)}"
: "${TOKEN:?set CF_TOKEN or ~/.cloudflared/cf_token to a scoped Cloudflare API token}"
API="https://api.cloudflare.com/client/v4/accounts/${CF_ACCOUNT_ID}/access"

APP_JSON=$(curl -fsS -X POST "$API/apps" \
  -H "Authorization: ******" -H "Content-Type: application/json" \
  -d "{\"name\":\"${UNFLINCHER_DOMAIN}\",\"domain\":\"${UNFLINCHER_DOMAIN}\",\"type\":\"self_hosted\",
       \"session_duration\":\"24h\",\"auto_redirect_to_identity\":false}")
APP_ID=$(echo "$APP_JSON" | jq -r '.result.id')
APP_AUD=$(echo "$APP_JSON" | jq -r '.result.aud')

curl -fsS -X POST "$API/apps/${APP_ID}/policies" \
  -H "Authorization: ******" -H "Content-Type: application/json" \
  -d "{\"name\":\"Allow ${UNFLINCHER_OPERATOR_EMAIL} via OTP\",\"decision\":\"allow\",\"precedence\":1,
       \"include\":[{\"email\":{\"email\":\"${UNFLINCHER_OPERATOR_EMAIL}\"}}]}" | jq '{policy_id:.result.id}'

echo "app_id=${APP_ID}"
echo "aud=${APP_AUD}   <- put this into unflincher.container's UNFLINCHER_CF_ACCESS_AUD"
