#!/usr/bin/env bash
# verify_cycle.sh -- prove the full gate-llm cycle on YOUR hardware:
#   asleep -> request -> wakes -> responds -> idle -> asleep again,
# measuring free VRAM (nvidia-smi) at each stage when available.
#
# Configuration (environment variables):
#   GATE_URL        Base URL of the gatekeeper port to test.
#                   Default: http://127.0.0.1:8080
#   REQUEST_PATH    Path requested through the proxy to wake + verify.
#                   Default: /health  (use a real API path for your service,
#                   e.g. /v1/models for an OpenAI-compatible LLM server)
#   REQUEST_METHOD  GET (default) or POST.
#   REQUEST_BODY    Optional request body for POST.
#   WAKE_TIMEOUT_S  Max seconds to wait for the cold response. Default: 300
#   SLEEP_WAIT_S    Max seconds to wait for the idle stop at the end.
#                   Default: the service's idle_timeout_s + 120. Set your
#                   service's idle window low (e.g. 30s) for a quick test,
#                   or accept a long wait with your production window.
#
# Exit code 0 = the whole cycle behaved as documented. 1 = a stage failed.
set -u
GATE_URL="${GATE_URL:-http://127.0.0.1:8080}"
REQUEST_PATH="${REQUEST_PATH:-/health}"
REQUEST_METHOD="${REQUEST_METHOD:-GET}"
REQUEST_BODY="${REQUEST_BODY:-}"
WAKE_TIMEOUT_S="${WAKE_TIMEOUT_S:-300}"
SLEEP_WAIT_S="${SLEEP_WAIT_S:-}"

STATUS_URL="$GATE_URL/__gate-llm/status"
fail() { echo "FAIL: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }

status_field() {
  curl -s -m 5 "$STATUS_URL" | python3 -c \
    "import json,sys; print(json.load(sys.stdin).get('$1',''))" 2>/dev/null
}

free_vram() {
  if command -v nvidia-smi >/dev/null 2>&1; then
    local mib
    mib=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null | head -1)
    [ -n "$mib" ] && echo "free VRAM: ${mib} MiB"
  fi
}

step "1/5 state before (querying status must NOT wake the service)"
state=$(status_field state) || fail "status endpoint unreachable at $STATUS_URL"
echo "state: $state"
free_vram
[ "$state" = "awake" ] && echo "note: service is already awake; the cold-wake timing below will read ~0"

step "2/5 cold request through the gatekeeper (the request waits, it does not fail)"
t0=$(date +%s.%N)
if [ "$REQUEST_METHOD" = "POST" ]; then
  code=$(curl -s -o /tmp/gate-llm-verify-body -w "%{http_code}" -m "$WAKE_TIMEOUT_S" \
           -X POST ${REQUEST_BODY:+-d "$REQUEST_BODY"} "$GATE_URL$REQUEST_PATH") \
    || fail "request errored or exceeded ${WAKE_TIMEOUT_S}s"
else
  code=$(curl -s -o /tmp/gate-llm-verify-body -w "%{http_code}" -m "$WAKE_TIMEOUT_S" \
           "$GATE_URL$REQUEST_PATH") || fail "request errored or exceeded ${WAKE_TIMEOUT_S}s"
fi
t1=$(date +%s.%N)
echo "HTTP $code in $(echo "$t1 $t0" | awk '{printf "%.2f", $1-$2}')s"
[ "$code" -ge 200 ] && [ "$code" -lt 500 ] || fail "unexpected HTTP status $code (body in /tmp/gate-llm-verify-body)"

step "3/5 state after the request"
state=$(status_field state)
echo "state: $state"
free_vram
[ "$state" = "awake" ] || fail "service should be awake after a successful request"

step "4/5 warm request (should be fast)"
t0=$(date +%s.%N)
curl -s -o /dev/null -m 60 "$GATE_URL$REQUEST_PATH" || fail "warm request failed"
t1=$(date +%s.%N)
echo "warm response in $(echo "$t1 $t0" | awk '{printf "%.2f", $1-$2}')s"

step "5/5 waiting for the idle stop"
idle=$(status_field idle_timeout_s)
if [ -z "$SLEEP_WAIT_S" ]; then
  SLEEP_WAIT_S=$(python3 -c "print(int(float('${idle:-0}') + 120))")
fi
echo "idle window: ${idle}s; waiting up to ${SLEEP_WAIT_S}s for the stop..."
waited=0
while [ "$waited" -lt "$SLEEP_WAIT_S" ]; do
  state=$(status_field state)
  [ "$state" = "asleep" ] && break
  sleep 10; waited=$((waited + 10))
done
echo "state: $state"
free_vram
[ "$state" = "asleep" ] || fail "service did not go back to sleep within ${SLEEP_WAIT_S}s"

echo
echo "PASS: asleep -> request -> awake -> responds -> idle -> asleep. The pattern works on this machine."
