#!/usr/bin/env python3
"""
run_all.py -- run the whole gate-llm test set and report per-test
results. This is the exact command the GitHub Actions workflow runs on
ubuntu-latest and windows-latest, and the command to run locally
before pushing:

    python tests/run_all.py

Every test is a self-contained script in this directory that exits 0
and prints PASS on success. No GPU is required anywhere in the set:
backends are fakes (tests/fake_backend.py) and the installer test runs
with --skip-gpu-check and the `process` residence strategy, so the
systemd and scheduled-task registration paths are NOT exercised here
-- they are exercised on real installs; the workflow file says so too.
"""

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = ["test_proxy_cycle.py", "test_bench.py", "test_menu.py",
         "test_install.py"]


def main():
    failures = []
    for name in TESTS:
        print(f"\n===== {name} =====", flush=True)
        t0 = time.time()
        out = subprocess.run([sys.executable, os.path.join(HERE, name)])
        elapsed = time.time() - t0
        if out.returncode == 0:
            print(f"----- {name}: OK ({elapsed:.1f}s)")
        else:
            print(f"----- {name}: FAILED (exit {out.returncode}, "
                  f"{elapsed:.1f}s)")
            failures.append(name)
    print()
    if failures:
        print(f"RESULT: {len(failures)} of {len(TESTS)} tests FAILED: "
              + ", ".join(failures))
        return 1
    print(f"RESULT: all {len(TESTS)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
