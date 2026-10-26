#!/usr/bin/env bash
# gate-llm -- one-command installer.
#
#   sudo ./install.sh
#
# What it does, idempotently:
#   1. checks prerequisites and FAILS LOUDLY, with the fix, when one is
#      missing (Python 3, systemctl, an NVIDIA GPU via nvidia-smi, and
#      Docker when the config you install drives containers);
#   2. installs the gatekeeper, tools and config template;
#   3. creates /etc/gate-llm/services.json from the example ONLY if none
#      exists -- your config is never overwritten;
#   4. installs and enables the systemd unit and starts the service;
#   5. verifies the pattern end to end against your first service:
#      asleep -> real request -> wakes -> responds, with timings.
#
# Flags:
#   --config FILE     install FILE as services.json (only if none exists yet)
#   --skip-gpu-check  skip the nvidia-smi check (CPU-only services, testing)
#   --no-verify       skip the final wake verification
#   -h, --help        this text
#
# Locations can be redirected with GATE_LLM_INSTALL_DIR, GATE_LLM_CONFIG_DIR
# and GATE_LLM_SYSTEMD_DIR (used by the sandboxed self-test; normally unset).
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${GATE_LLM_INSTALL_DIR:-/opt/gate-llm}"
CONFIG_DIR="${GATE_LLM_CONFIG_DIR:-/etc/gate-llm}"
SYSTEMD_DIR="${GATE_LLM_SYSTEMD_DIR:-/etc/systemd/system}"
CONFIG_FILE="$CONFIG_DIR/services.json"
SERVICE_NAME="gate-llm"
USER_CONFIG=""
SKIP_GPU=0
NO_VERIFY=0

info() { echo "==> $*"; }
die()  { echo "ERROR: $*" >&2; echo "Installation aborted; nothing was started." >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --config) USER_CONFIG="${2:-}"; shift 2 ;;
    --skip-gpu-check) SKIP_GPU=1; shift ;;
    --no-verify) NO_VERIFY=1; shift ;;
    -h|--help) sed -n '2,30p' "$0" | sed 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

# ---------------------------------------------------------------- checks
info "Checking prerequisites"
command -v python3 >/dev/null 2>&1 || die "python3 not found. Install Python 3.8+ (e.g. sudo apt install python3) and re-run."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
  || die "Python 3.8+ is required; found $(python3 --version 2>&1)."
command -v systemctl >/dev/null 2>&1 || die "systemctl not found. gate-llm runs as a systemd service; use a systemd-based distro or run gate_llm_proxy.py by hand (see README, manual install)."

if [ "$SKIP_GPU" -eq 0 ]; then
  command -v nvidia-smi >/dev/null 2>&1 || die "nvidia-smi not found: no NVIDIA GPU detected. Install the NVIDIA driver, or re-run with --skip-gpu-check if every service you register is CPU-only."
  GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || true)"
  [ -n "$GPU_NAME" ] || die "nvidia-smi runs but reports no GPU. Check the driver (nvidia-smi) and re-run."
  info "GPU detected: $GPU_NAME"
else
  info "GPU check skipped (--skip-gpu-check)"
fi

[ -f "$SRC_DIR/gate_llm_proxy.py" ] || die "gate_llm_proxy.py not found next to install.sh; run the installer from the gate-llm repository root."
[ -f "$SRC_DIR/services.example.json" ] || die "services.example.json not found next to install.sh; incomplete checkout?"

# ---------------------------------------------------------------- files
info "Installing files into $INSTALL_DIR"
mkdir -p "$INSTALL_DIR/tools" "$CONFIG_DIR" "$SYSTEMD_DIR"
cp "$SRC_DIR/gate_llm_proxy.py" "$INSTALL_DIR/"
cp "$SRC_DIR/tools/bench.py" "$SRC_DIR/tools/menu.py" "$SRC_DIR/tools/verify_cycle.sh" "$INSTALL_DIR/tools/"
chmod +x "$INSTALL_DIR/tools/verify_cycle.sh"

if [ -f "$CONFIG_FILE" ]; then
  info "Config already exists at $CONFIG_FILE -- keeping it (never overwritten)"
else
  if [ -n "$USER_CONFIG" ]; then
    [ -f "$USER_CONFIG" ] || die "--config file not found: $USER_CONFIG"
    cp "$USER_CONFIG" "$CONFIG_FILE"
    info "Installed your config as $CONFIG_FILE"
  else
    cp "$SRC_DIR/services.example.json" "$CONFIG_FILE"
    info "Created $CONFIG_FILE from the example -- EDIT IT to match your services"
  fi
fi
python3 "$INSTALL_DIR/gate_llm_proxy.py" --config "$CONFIG_FILE" --check \
  || die "the config at $CONFIG_FILE is invalid (see the errors above); fix it and re-run."

if grep -q '"docker"' "$CONFIG_FILE"; then
  command -v docker >/dev/null 2>&1 || die "your config drives docker containers but the docker CLI was not found. Install Docker, or rewrite the start/stop commands in $CONFIG_FILE."
  docker info >/dev/null 2>&1 || die "the docker CLI exists but the daemon is not reachable (docker info failed). Start Docker and re-run."
  info "Docker check passed (config drives containers)"
fi

info "Installing the systemd unit"
sed -e "s|/opt/gate-llm|$INSTALL_DIR|g" -e "s|/etc/gate-llm|$CONFIG_DIR|g" \
    "$SRC_DIR/gate-llm.service" > "$SYSTEMD_DIR/$SERVICE_NAME.service"

# ---------------------------------------------------------------- service
info "Enabling and starting $SERVICE_NAME"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || die "systemctl enable failed; try running the installer with sudo."
systemctl restart "$SERVICE_NAME" || die "systemctl restart failed; inspect with: systemctl status $SERVICE_NAME --no-pager"

FIRST_PORT="$(python3 -c "import json; print(json.load(open('$CONFIG_FILE'))['services'][0]['listen_port'])")"
FIRST_HEALTH_PATH="$(python3 -c "
import json
from urllib.parse import urlsplit
svc = json.load(open('$CONFIG_FILE'))['services'][0]
print(urlsplit(svc.get('health_url', 'http://x/health')).path or '/health')")"

info "Waiting for the gatekeeper on port $FIRST_PORT"
ok=0
for _ in $(seq 1 30); do
  if curl -s -m 2 "http://127.0.0.1:$FIRST_PORT/__gate-llm/status" >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
[ "$ok" -eq 1 ] || die "the gatekeeper does not answer on port $FIRST_PORT; inspect with: journalctl -u $SERVICE_NAME -n 50 --no-pager"

# ---------------------------------------------------------------- verify
if [ "$NO_VERIFY" -eq 0 ]; then
  info "Verifying the cycle on the first service: asleep -> request -> wakes -> responds"
  state="$(curl -s "http://127.0.0.1:$FIRST_PORT/__gate-llm/status" | python3 -c "import json,sys; print(json.load(sys.stdin)['state'])")"
  if [ "$state" = "awake" ]; then
    curl -s -X POST "http://127.0.0.1:$FIRST_PORT/__gate-llm/sleep" >/dev/null || true
    state="$(curl -s "http://127.0.0.1:$FIRST_PORT/__gate-llm/status" | python3 -c "import json,sys; print(json.load(sys.stdin)['state'])")"
  fi
  echo "    state before: $state"
  t0="$(date +%s.%N)"
  code="$(curl -s -o /dev/null -w '%{http_code}' -m 600 "http://127.0.0.1:$FIRST_PORT$FIRST_HEALTH_PATH")" \
    || die "verification request failed; the backend did not wake/respond in time. Check: journalctl -u $SERVICE_NAME -n 50 --no-pager"
  t1="$(date +%s.%N)"
  state="$(curl -s "http://127.0.0.1:$FIRST_PORT/__gate-llm/status" | python3 -c "import json,sys; print(json.load(sys.stdin)['state'])")"
  echo "    cold request: HTTP $code in $(echo "$t1 $t0" | awk '{printf "%.1f", $1-$2}')s; state after: $state"
  [ "$state" = "awake" ] || die "verification failed: service is not awake after a successful request."
  echo "    (the backend now sleeps again after its idle window; run"
  echo "     $INSTALL_DIR/tools/verify_cycle.sh for the full unattended cycle)"
else
  info "Skipping the wake verification (--no-verify)"
fi

cat <<EOF

gate-llm is installed and running.

  Config:   $CONFIG_FILE   (edit it, then: sudo systemctl restart $SERVICE_NAME)
  Manage:   python3 $INSTALL_DIR/tools/menu.py --config $CONFIG_FILE
  Bench:    python3 $INSTALL_DIR/tools/bench.py --base-url http://127.0.0.1:$FIRST_PORT
  Logs:     journalctl -u $SERVICE_NAME -f
EOF
