#!/usr/bin/env python3
"""
fake_daemon.py -- a stand-in for `docker start` / `docker stop`, so the
test suite can exercise the gatekeeper's *command* supervision strategy
(daemon-style start/stop command pairs) on machines without Docker --
including the GitHub Actions runners.

    fake_daemon.py start --pidfile F -- <backend command...>
    fake_daemon.py stop  --pidfile F

`start` spawns the backend detached (through gate_llm_env, like the
gatekeeper's own process strategy), records its PID in F and returns
immediately -- exactly the contract `docker start` offers. `stop`
terminates the recorded PID. Run it with the repository root's Python
path context: it imports gate_llm_env from the repo root.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import gate_llm_env as genv  # noqa: E402


def main(argv):
    if len(argv) < 4 or argv[2] != "--pidfile":
        print("usage: fake_daemon.py start|stop --pidfile F [-- cmd...]",
              file=sys.stderr)
        return 2
    action, pidfile = argv[1], argv[3]
    if action == "start":
        if len(argv) < 6 or argv[4] != "--":
            print("start needs '--' followed by the backend command",
                  file=sys.stderr)
            return 2
        backend = argv[5:]
        proc = genv.spawn_detached(backend)
        genv.write_pid_record(pidfile, proc.pid, backend)
        return 0
    if action == "stop":
        record = genv.read_pid_record(pidfile)
        if record:
            genv.terminate_pid(record["pid"], grace_s=10)
            genv.clear_pid_record(pidfile)
        return 0
    print(f"unknown action: {action}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
