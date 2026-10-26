#!/usr/bin/env bash
# verify_cycle.sh -- POSIX shim. The cycle verification itself lives in
# verify_cycle.py (Python standard library) so it runs identically on
# Windows and Linux; this script only forwards to it. Configuration is
# by the same environment variables (GATE_URL, REQUEST_PATH, ...),
# documented in verify_cycle.py.
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/verify_cycle.py" "$@"
