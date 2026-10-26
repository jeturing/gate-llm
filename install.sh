#!/usr/bin/env bash
# gate-llm -- POSIX shortcut for the installer.
#
# All installer logic lives in install.py (Python standard library),
# which runs identically on Windows and Linux: environment detection,
# prerequisite checks, config creation, residence registration
# (systemd / Windows scheduled task / detached process) and the final
# cycle verification. This shim exists only so the documented POSIX
# flow (`sudo ./install.sh`) keeps working; on Windows run
# `python install.py` directly. Flags are install.py's flags
# (--config, --skip-gpu-check, --no-verify, --strategy, ...).
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install.py" "$@"
