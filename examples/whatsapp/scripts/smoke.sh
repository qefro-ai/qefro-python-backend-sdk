#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-8089}"
QEFRO_SIGNING_SECRET="${QEFRO_SIGNING_SECRET:-dev-secret}"
BASE_URL="${BASE_URL:-http://127.0.0.1:${PORT}/qefro}"

sign_body() {
  local ts="$1"
  local body="$2"
  local payload="v1:${ts}:${body}"
  local hex
  hex=$(printf '%s' "${payload}" | openssl dgst -sha256 -hmac "${QEFRO_SIGNING_SECRET}" -binary | xxd -p -c 256)
  printf 'v1=%s' "${hex}"
}

send_signed() {
  local body="$1"
  local ts sig
  ts=$(date +%s)
  sig=$(sign_body "${ts}" "${body}")
  curl -sS -X POST "${BASE_URL}" \
    -H "Content-Type: application/json" \
    -H "X-Qefro-Protocol: 1" \
    -H "X-Qefro-Timestamp: ${ts}" \
    -H "X-Qefro-Signature: ${sig}" \
    -d "${body}"
}

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "${SERVER_PID}" 2>/dev/null; then
    kill "${SERVER_PID}" >/dev/null 2>&1 || true
    wait "${SERVER_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXAMPLE_DIR="$(dirname "${SCRIPT_DIR}")"
REPO_DIR="$(dirname "$(dirname "${EXAMPLE_DIR}")")"

cd "${REPO_DIR}"
source .venv/bin/activate
python "${EXAMPLE_DIR}/server.py" >/tmp/qefro-smoke-py-whatsapp.log 2>&1 &
SERVER_PID=$!

for _ in {1..25}; do
  if curl -sS -o /dev/null -X POST "${BASE_URL}" --max-time 1 2>/dev/null; then
    break
  fi
  sleep 0.2
done

echo "=== 1. Ping ==="
PING_BODY='{"protocol_version":"1","request_id":"smoke-ping","type":"ping"}'
PING_RESPONSE=$(send_signed "${PING_BODY}")
echo "${PING_RESPONSE}"
echo "${PING_RESPONSE}" | grep -q '"type":"pong"'

echo "=== 2. Tools List ==="
LIST_BODY='{"protocol_version":"1","request_id":"smoke-list","type":"tools.list"}'
LIST_RESPONSE=$(send_signed "${LIST_BODY}")
echo "${LIST_RESPONSE}" | grep -q '"name":"whatsapp_send_template"'
echo "${LIST_RESPONSE}" | grep -q '"name":"whatsapp_launch_flow"'

echo "=== 3. Capabilities List (WhatsApp Capability) ==="
CAPS_BODY='{"protocol_version":"1","request_id":"smoke-caps","type":"capabilities.list"}'
CAPS_RESPONSE=$(send_signed "${CAPS_BODY}")
echo "${CAPS_RESPONSE}" | grep -q '"whatsapp":'
echo "${CAPS_RESPONSE}" | grep -q '"order_update"'
echo "${CAPS_RESPONSE}" | grep -q '"book_viewing"'

echo "=== 4. Invoke wa_template_preview ==="
PREVIEW_BODY='{"protocol_version":"1","request_id":"smoke-prev","type":"tool.invoke","conversation_id":"smoke-conv","identity":{"customer_id":"demo-customer","phone":"+15550001111"},"tool":"wa_template_preview","parameters":{"template":"order_update"}}'
PREVIEW_RESPONSE=$(send_signed "${PREVIEW_BODY}")
echo "${PREVIEW_RESPONSE}"
echo "${PREVIEW_RESPONSE}" | grep -q '"type":"result"'
echo "${PREVIEW_RESPONSE}" | grep -q '"approved":true'

echo "=== 5. Invoke whatsapp_send_template (missing workspace channel settings) ==="
SEND_BODY_NOCREDS='{"protocol_version":"1","request_id":"smoke-send-nocreds","type":"tool.invoke","conversation_id":"smoke-conv","identity":{"phone":"+15550001111"},"tool":"whatsapp_send_template","parameters":{"to":"+15550001111","template":"order_update","parameters":{"customer_name":"Alice","order_id":"ORD-101","status":"shipped"}}}'
SEND_RESP_NOCREDS=$(send_signed "${SEND_BODY_NOCREDS}")
echo "${SEND_RESP_NOCREDS}"
echo "${SEND_RESP_NOCREDS}" | grep -q '"code":"missing_credentials"'

echo "=== 6. Invoke whatsapp_send_template (with Qefro workspace channel settings injected) ==="
SEND_BODY_SETTINGS='{"protocol_version":"1","request_id":"smoke-send-settings","type":"tool.invoke","conversation_id":"smoke-conv","identity":{"phone":"+15550001111"},"settings":{"whatsapp":{"phone_number_id":"1179902118548695","access_token":"dev-token"}},"tool":"whatsapp_send_template","parameters":{"to":"+15550001111","template":"order_update","parameters":{"customer_name":"Alice","order_id":"ORD-101","status":"shipped"}}}'
SEND_RESP_SETTINGS=$(send_signed "${SEND_BODY_SETTINGS}")
echo "${SEND_RESP_SETTINGS}"
echo "${SEND_RESP_SETTINGS}" | grep -q '"template":"order_update"'

echo "=== 7. Invoke whatsapp_launch_flow (with Qefro workspace channel settings injected) ==="
FLOW_BODY='{"protocol_version":"1","request_id":"smoke-flow","type":"tool.invoke","conversation_id":"smoke-conv","identity":{"phone":"+15550001111"},"settings":{"whatsapp":{"phone_number_id":"1179902118548695","access_token":"dev-token"}},"tool":"whatsapp_launch_flow","parameters":{"to":"+15550001111","flow_id":"book_viewing"}}'
FLOW_RESPONSE=$(send_signed "${FLOW_BODY}")
echo "${FLOW_RESPONSE}"
echo "${FLOW_RESPONSE}" | grep -q '"flow_id":"book_viewing"'

echo ""
echo "All WhatsApp smoke tests passed successfully for Python SDK!"
