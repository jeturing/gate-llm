#!/usr/bin/env python3
"""
test_bench.py -- tools/bench.py against the calibrated fake backend:
the fake streams 24 chunks at a 0.05s interval (~20 tok/s), so the
bench must report a sane TTFT and a generation speed in a loose band
around the calibration (CI machines vary; the band is generous on
purpose -- this tests the measurement plumbing, not the machine).

Exits 0 and prints PASS. Standard library.
"""

import json
import os
import subprocess
import sys
import time
import socket

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import gate_llm_env as genv  # noqa: E402


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def main():
    port = free_port()
    backend = genv.spawn_detached(
        [sys.executable, os.path.join(HERE, "fake_backend.py"),
         "--port", str(port), "--load-seconds", "0",
         "--chunk-interval", "0.05", "--chunks", "24"])
    try:
        # wait until it serves
        import urllib.request
        deadline = time.time() + 15
        while True:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health", timeout=2) as resp:
                    if resp.status == 200:
                        break
            except Exception:
                if time.time() > deadline:
                    raise AssertionError("fake backend never came up")
                time.sleep(0.3)
        out = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "bench.py"),
             "--base-url", f"http://127.0.0.1:{port}",
             "--model", "fake-1", "--runs", "2", "--json"],
            capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, f"bench failed:\n{out.stderr}"
        summary = json.loads(out.stdout)
        assert summary["runs"] == 2, summary
        assert summary["ttft_median_s"] >= 0, summary
        tok_s = summary["gen_tok_s_median"]
        assert 3 <= tok_s <= 80, f"implausible tok/s from bench: {tok_s}"
        assert all(r["tokens"] == 24 for r in summary["results"]), summary
        print(f"  bench: TTFT median {summary['ttft_median_s']}s, "
              f"{tok_s} tok/s (fake calibrated ~20 tok/s)")
    finally:
        genv.terminate_pid(backend.pid, grace_s=5)
    print("PASS test_bench")
    return 0


if __name__ == "__main__":
    sys.exit(main())
