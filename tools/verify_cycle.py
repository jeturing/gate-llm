#!/usr/bin/env python3
"""
verify_cycle.py -- prove the full gate-llm cycle on YOUR hardware:
  asleep -> request -> wakes -> responds -> idle -> asleep again,
measuring free VRAM (via nvidia-smi, wherever it lives) at each stage
when a GPU is available.

Runs identically on Windows and Linux -- this is the portable twin of
verify_cycle.sh, which now simply delegates here.

Configuration (environment variables, same names as the shell version):
  GATE_URL        Base URL of the gatekeeper port to test.
                  Default: http://127.0.0.1:8080
  REQUEST_PATH    Path requested through the proxy to wake + verify.
                  Default: /health  (use a real API path for your
                  service, e.g. /v1/models for an OpenAI-compatible
                  LLM server)
  REQUEST_METHOD  GET (default) or POST.
  REQUEST_BODY    Optional request body for POST.
  WAKE_TIMEOUT_S  Max seconds to wait for the cold response. Default: 300
  SLEEP_WAIT_S    Max seconds to wait for the idle stop at the end.
                  Default: the service's idle_timeout_s + 120. Set the
                  service's idle window low (e.g. 30s) for a quick
                  test, or accept a long wait with a production window.

Exit code 0 = the whole cycle behaved as documented. 1 = a stage failed.

Pure Python 3 standard library.
"""

import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import gate_llm_env as genv  # noqa: E402

GATE_URL = os.environ.get("GATE_URL", "http://127.0.0.1:8080").rstrip("/")
REQUEST_PATH = os.environ.get("REQUEST_PATH", "/health")
REQUEST_METHOD = os.environ.get("REQUEST_METHOD", "GET").upper()
REQUEST_BODY = os.environ.get("REQUEST_BODY", "")
WAKE_TIMEOUT_S = float(os.environ.get("WAKE_TIMEOUT_S", "300"))
SLEEP_WAIT_S = os.environ.get("SLEEP_WAIT_S", "")
STATUS_URL = GATE_URL + "/__gate-llm/status"


def fail(msg):
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def step(title):
    print()
    print(f"== {title}")


def http(url, method="GET", body=None, timeout=10):
    req = urllib.request.Request(url, method=method, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def status_field(field):
    try:
        _, raw = http(STATUS_URL, timeout=5)
        return json.loads(raw.decode("utf-8")).get(field, "")
    except Exception:
        return None


def free_vram():
    totals = genv.gpu_totals()
    if totals:
        print(f"free VRAM: {totals[0]:,} MiB")


def request_through_gate(timeout):
    body = None
    if REQUEST_METHOD == "POST":
        body = REQUEST_BODY.encode("utf-8") if REQUEST_BODY else b""
    code, raw = http(GATE_URL + REQUEST_PATH, method=REQUEST_METHOD,
                     body=body, timeout=timeout)
    fd, path = tempfile.mkstemp(prefix="gate-llm-verify-body-")
    with os.fdopen(fd, "wb") as fh:
        fh.write(raw or b"")
    return code, path


def main():
    step("1/5 state before (querying status must NOT wake the service)")
    state = status_field("state")
    if state is None:
        fail(f"status endpoint unreachable at {STATUS_URL}")
    print(f"state: {state}")
    free_vram()
    if state == "awake":
        print("note: service is already awake; the cold-wake timing "
              "below will read ~0")

    step("2/5 cold request through the gatekeeper "
         "(the request waits, it does not fail)")
    t0 = time.time()
    try:
        code, body_path = request_through_gate(WAKE_TIMEOUT_S)
    except Exception as exc:
        fail(f"request errored or exceeded {WAKE_TIMEOUT_S:.0f}s: {exc}")
    elapsed = time.time() - t0
    print(f"HTTP {code} in {elapsed:.2f}s")
    if not (200 <= code < 500):
        fail(f"unexpected HTTP status {code} (body in {body_path})")

    step("3/5 state after the request")
    state = status_field("state")
    print(f"state: {state}")
    free_vram()
    if state != "awake":
        fail("service should be awake after a successful request")

    step("4/5 warm request (should be fast)")
    t0 = time.time()
    try:
        http(GATE_URL + REQUEST_PATH, method=REQUEST_METHOD,
             body=(REQUEST_BODY.encode("utf-8")
                   if REQUEST_METHOD == "POST" and REQUEST_BODY else
                   (b"" if REQUEST_METHOD == "POST" else None)),
             timeout=60)
    except Exception as exc:
        fail(f"warm request failed: {exc}")
    print(f"warm response in {time.time() - t0:.2f}s")

    step("5/5 waiting for the idle stop")
    idle = status_field("idle_timeout_s")
    if SLEEP_WAIT_S:
        sleep_wait = float(SLEEP_WAIT_S)
    else:
        try:
            sleep_wait = float(idle) + 120
        except (TypeError, ValueError):
            sleep_wait = 120
    print(f"idle window: {idle}s; waiting up to {sleep_wait:.0f}s "
          "for the stop...")
    waited = 0.0
    state = status_field("state")
    while waited < sleep_wait and state != "asleep":
        time.sleep(10)
        waited += 10
        state = status_field("state")
    print(f"state: {state}")
    free_vram()
    if state != "asleep":
        fail(f"service did not go back to sleep within {sleep_wait:.0f}s")

    print()
    print("PASS: asleep -> request -> awake -> responds -> idle -> "
          "asleep. The pattern works on this machine.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
