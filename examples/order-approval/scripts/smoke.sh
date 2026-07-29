#!/usr/bin/env bash
set -euo pipefail

# Smoke test: boot the order-approval server, send a signed ping and a signed
# capabilities.list, and assert both flows (and the approval + auth tool step)
# are advertised. Mirrors the JS/Rust SDK smoke tests.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-8093}"
QEFRO_SIGNING_SECRET="${QEFRO_SIGNING_SECRET:-dev-secret}"
BASE_URL="${BASE_URL:-http://127.0.0.1:${PORT}/qefro}"
PYTHON="${PYTHON:-python}"

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

export QEFRO_SIGNING_SECRET PORT
"${PYTHON}" "${HERE}/server.py" >/tmp/qefro-smoke-order-approval-py.log 2>&1 &
SERVER_PID=$!

for _ in {1..25}; do
  if curl -sS -o /dev/null -X POST "${BASE_URL}" --max-time 1; then
    break
  fi
  sleep 0.2
done

PING_BODY='{"protocol_version":"1","request_id":"smoke-ping","type":"ping"}'
CAPABILITIES_BODY='{"protocol_version":"1","request_id":"smoke-capabilities","type":"capabilities.list"}'

PING_RESPONSE=$(send_signed "${PING_BODY}")
CAPABILITIES_RESPONSE=$(send_signed "${CAPABILITIES_BODY}")

echo "Ping response: ${PING_RESPONSE}"
echo "Capabilities response: ${CAPABILITIES_RESPONSE}"

echo "${PING_RESPONSE}" | grep -q '"type":"pong"'
echo "${CAPABILITIES_RESPONSE}" | grep -q '"type":"capabilities.list"'
echo "${CAPABILITIES_RESPONSE}" | grep -q '"id":"track-order"'
echo "${CAPABILITIES_RESPONSE}" | grep -q '"id":"cancel-order"'
echo "${CAPABILITIES_RESPONSE}" | grep -q '"type":"approval"'
echo "${CAPABILITIES_RESPONSE}" | grep -q '"tool_ref":"order_cancel"'

echo "Smoke test passed for order-approval"
