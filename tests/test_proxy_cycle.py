#!/usr/bin/env python3
"""
test_proxy_cycle.py -- the gatekeeper's full cycle against fake
backends, on whatever OS runs this file:

  * service A uses the `process` supervision strategy (the gatekeeper
    spawns the backend itself, PID record and all);
  * service B uses the legacy `command` strategy with a daemon-style
    start/stop pair (tests/fake_daemon.py standing in for docker).

For each: asleep -> cold request (held while the backend "loads") ->
awake -> manual sleep -> wake endpoint -> idle window -> asleep again,
with the backend process verifiably gone at the end.

Exits 0 and prints PASS when every assertion holds. Standard library.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import gate_llm_env as genv  # noqa: E402

PY = sys.executable
FAKE = os.path.join(HERE, "fake_backend.py")
DAEMON = os.path.join(HERE, "fake_daemon.py")


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def http(url, method="GET", timeout=10):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def status(port):
    code, raw = http(f"http://127.0.0.1:{port}/__gate-llm/status", timeout=5)
    assert code == 200, f"status endpoint answered {code}"
    return json.loads(raw.decode("utf-8"))


def wait_for(predicate, timeout_s, what):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.4)
    raise AssertionError(f"timed out waiting for: {what}")


def port_answers(port):
    try:
        code, _ = http(f"http://127.0.0.1:{port}/health", timeout=1.5)
        return code in (200, 503)
    except Exception:
        return False


def exercise(port, backend_port, label):
    """The shared cycle assertions for one service."""
    st = status(port)
    assert st["state"] == "asleep", f"{label}: expected asleep, got {st}"
    assert not port_answers(backend_port), f"{label}: backend already up"

    # Cold request: held while the fake loads (2s), then served.
    t0 = time.time()
    code, _ = http(f"http://127.0.0.1:{port}/v1/models", timeout=60)
    cold_s = time.time() - t0
    assert code == 200, f"{label}: cold request -> HTTP {code}"
    assert cold_s >= 1.2, f"{label}: cold request was not held ({cold_s:.2f}s)"
    st = status(port)
    assert st["state"] == "awake", f"{label}: not awake after request: {st}"
    print(f"  {label}: cold request held {cold_s:.2f}s -> awake "
          f"(supervision={st.get('supervision')}, pid={st.get('backend_pid')})")

    # Manual sleep through the built-in endpoint.
    code, _ = http(f"http://127.0.0.1:{port}/__gate-llm/sleep",
                   method="POST", timeout=60)
    assert code == 200, f"{label}: sleep endpoint -> HTTP {code}"
    wait_for(lambda: status(port)["state"] == "asleep", 20,
             f"{label} asleep after manual sleep")
    wait_for(lambda: not port_answers(backend_port), 20,
             f"{label} backend process gone after manual sleep")

    # Wake endpoint, then let the idle window (3s) put it back to sleep.
    code, raw = http(f"http://127.0.0.1:{port}/__gate-llm/wake",
                     method="POST", timeout=60)
    assert code == 200, f"{label}: wake endpoint -> HTTP {code}: {raw!r}"
    assert status(port)["state"] == "awake"
    wait_for(lambda: status(port)["state"] == "asleep", 40,
             f"{label} asleep after idle window")
    wait_for(lambda: not port_answers(backend_port), 20,
             f"{label} backend process gone after idle stop")
    print(f"  {label}: wake endpoint + idle stop OK")


def main():
    tmp = tempfile.mkdtemp(prefix="gate-llm-cycle-")
    listen_a, backend_a = free_port(), free_port()
    listen_b, backend_b = free_port(), free_port()
    daemon_pidfile = os.path.join(tmp, "fake-daemon.pid.json")
    cfg = {
        "services": [
            {
                "name": "fake-process",
                "listen_host": "127.0.0.1", "listen_port": listen_a,
                "backend_host": "127.0.0.1", "backend_port": backend_a,
                "health_url": f"http://127.0.0.1:{backend_a}/health",
                "health_expect_status": [200],
                "health_timeout_s": 60,
                "idle_timeout_s": 3,
                "stop_grace_s": 5,
                "supervision": "process",
                "start": [PY, FAKE, "--port", str(backend_a),
                          "--load-seconds", "2"],
            },
            {
                "name": "fake-command",
                "listen_host": "127.0.0.1", "listen_port": listen_b,
                "backend_host": "127.0.0.1", "backend_port": backend_b,
                "health_url": f"http://127.0.0.1:{backend_b}/health",
                "health_expect_status": [200],
                "health_timeout_s": 60,
                "idle_timeout_s": 3,
                "stop_grace_s": 5,
                "start": [PY, DAEMON, "start", "--pidfile", daemon_pidfile,
                          "--", PY, FAKE, "--port", str(backend_b),
                          "--load-seconds", "2"],
                "stop": [PY, DAEMON, "stop", "--pidfile", daemon_pidfile],
            },
        ]
    }
    cfg_path = os.path.join(tmp, "services.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)

    log_path = os.path.join(tmp, "gatekeeper.log")
    log_fh = open(log_path, "wb")
    proxy = subprocess.Popen(
        [PY, os.path.join(ROOT, "gate_llm_proxy.py"), "--config", cfg_path],
        stdout=log_fh, stderr=subprocess.STDOUT)
    try:
        wait_for(lambda: _status_or_none(listen_a) is not None, 20,
                 "gatekeeper status endpoint")
        print(f"  gatekeeper up (pid {proxy.pid}); log: {log_path}")
        exercise(listen_a, backend_a, "process strategy")
        # The process strategy must leave a clean state dir behind:
        state_dir = genv.state_dir_for(cfg_path)
        leftovers = [f for f in os.listdir(state_dir)
                     if f.endswith(".pid.json")] \
            if os.path.isdir(state_dir) else []
        assert not leftovers, f"PID records left behind: {leftovers}"
        exercise(listen_b, backend_b, "command strategy")
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proxy.kill()
        log_fh.close()
        # Belt and braces: no fake backend may survive the test.
        for port in (backend_a, backend_b):
            if port_answers(port):
                print(f"  cleanup note - backend on {port} still up after test",
                      file=sys.stderr)
    print("PASS test_proxy_cycle")
    return 0


def _status_or_none(port):
    try:
        return status(port)
    except Exception:
        return None


if __name__ == "__main__":
    sys.exit(main())
