#!/usr/bin/env python3
"""
test_menu.py -- tools/menu.py driven by a scripted stdin session
against a temporary config (no gatekeeper running):

  show table -> option 4 (idle window) -> service 1 -> 45 minutes
  -> option 0 (exit)

Asserts the config on disk now carries idle_timeout_s = 2700 and that
a .bak of the previous config was kept. Exits 0 and prints PASS.
Standard library.
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def main():
    tmp = tempfile.mkdtemp(prefix="gate-llm-menu-")
    cfg_path = os.path.join(tmp, "services.json")
    cfg = {"services": [{
        "name": "demo",
        "description": "menu test service",
        "listen_host": "127.0.0.1", "listen_port": 18080,
        "backend_host": "127.0.0.1", "backend_port": 18081,
        "health_url": "http://127.0.0.1:18081/health",
        "health_expect_status": [200],
        "health_timeout_s": 60,
        "idle_timeout_s": 1200,
        "stop_grace_s": 30,
        "docker_container": "demo-box",
        "supervision": "docker",
    }]}
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)

    script = "4\n1\n45\n0\n"
    out = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "menu.py"),
         "--config", cfg_path],
        input=script, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"menu exited {out.returncode}:\n{out.stderr}"
    assert "Bye." in out.stdout, out.stdout[-500:]

    with open(cfg_path, "r", encoding="utf-8") as fh:
        saved = json.load(fh)
    idle = saved["services"][0]["idle_timeout_s"]
    assert idle == 2700, f"idle window not updated on disk: {idle}"
    assert os.path.isfile(cfg_path + ".bak"), "config .bak was not kept"
    print("  menu: idle window edited to 45m, saved atomically, .bak kept")
    print("PASS test_menu")
    return 0


if __name__ == "__main__":
    sys.exit(main())
