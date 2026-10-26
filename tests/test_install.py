#!/usr/bin/env python3
"""
test_install.py -- install.py end to end in a sandbox, on whatever OS
runs this file:

  * locations redirected into a temp dir (GATE_LLM_*_DIR),
  * residence strategy `process` (no systemd, no scheduled task, no
    admin rights needed -- the same path Colab and Windows-without-
    elevation take),
  * GPU check skipped (the fake services are CPU-only),
  * a config whose only service is the fake backend, so the
    installer's own final verification performs a real cold wake.

Runs the installer TWICE: the second run must keep the existing
config byte for byte (never overwritten) and restart the detached
gatekeeper cleanly. Exits 0 and prints PASS. Standard library.
"""

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import gate_llm_env as genv  # noqa: E402

PY = sys.executable
FAKE = os.path.join(HERE, "fake_backend.py")


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def status(port):
    with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/__gate-llm/status", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    tmp = tempfile.mkdtemp(prefix="gate-llm-install-")
    install_dir = os.path.join(tmp, "install")
    config_dir = os.path.join(tmp, "config")
    listen_port, backend_port = free_port(), free_port()

    user_cfg = {
        "services": [{
            "name": "fake",
            "listen_host": "127.0.0.1", "listen_port": listen_port,
            "backend_host": "127.0.0.1", "backend_port": backend_port,
            "health_url": f"http://127.0.0.1:{backend_port}/health",
            "health_expect_status": [200],
            "health_timeout_s": 60,
            "idle_timeout_s": 600,
            "stop_grace_s": 5,
            "supervision": "process",
            "start": [PY, FAKE, "--port", str(backend_port),
                      "--load-seconds", "1"],
        }]
    }
    user_cfg_path = os.path.join(tmp, "myservices.json")
    with open(user_cfg_path, "w", encoding="utf-8") as fh:
        json.dump(user_cfg, fh)

    env = dict(os.environ)
    env["GATE_LLM_INSTALL_DIR"] = install_dir
    env["GATE_LLM_CONFIG_DIR"] = config_dir
    env["GATE_LLM_SYSTEMD_DIR"] = os.path.join(tmp, "systemd")

    def run_installer():
        out = subprocess.run(
            [PY, os.path.join(ROOT, "install.py"),
             "--config", user_cfg_path,
             "--skip-gpu-check", "--strategy", "process"],
            capture_output=True, text=True, env=env, timeout=300)
        assert out.returncode == 0, \
            f"installer failed:\n{out.stdout}\n{out.stderr}"
        return out.stdout

    installed_cfg = os.path.join(config_dir, "services.json")
    record_path = os.path.join(config_dir, "gatekeeper.pid.json")
    try:
        run_installer()
        for rel in ("gate_llm_proxy.py", "gate_llm_env.py",
                    os.path.join("tools", "menu.py"),
                    os.path.join("tools", "verify_cycle.py")):
            assert os.path.isfile(os.path.join(install_dir, rel)), \
                f"missing installed file: {rel}"
        assert os.path.isfile(installed_cfg), "config not installed"
        st = status(listen_port)
        assert st["state"] == "awake", \
            f"installer's verification should leave it awake: {st}"
        with open(installed_cfg, "rb") as fh:
            digest_before = hashlib.sha256(fh.read()).hexdigest()
        print("  install #1: files + config in place, verification "
              "woke the fake service")

        run_installer()  # idempotent; config preserved; gatekeeper restarted
        with open(installed_cfg, "rb") as fh:
            digest_after = hashlib.sha256(fh.read()).hexdigest()
        assert digest_before == digest_after, \
            "second install overwrote the existing config"
        st = status(listen_port)
        assert st["state"] in ("awake", "asleep"), st
        print("  install #2: config byte-identical, gatekeeper restarted "
              "and answering")
    finally:
        # Cleanup: sleep the backend, then stop the detached gatekeeper.
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{listen_port}/__gate-llm/sleep",
                method="POST")
            urllib.request.urlopen(req, timeout=30).read()
        except Exception:
            pass
        record = genv.read_pid_record(record_path)
        if record:
            genv.terminate_pid(record["pid"], grace_s=10)
    print("PASS test_install")
    return 0


if __name__ == "__main__":
    sys.exit(main())
