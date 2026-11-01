#!/usr/bin/env python3
"""
test_panel.py -- the read-only panel: status normalisation for both
gatekeeper dialects, counter accumulation over a scripted poll
sequence (fake clock, fake fetcher, fake GPU), the economics math,
and state persistence across a restart. No GPU, no network.
"""

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))

import panel as P  # noqa: E402

FAIL = []


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        FAIL.append(name)


# -- normalisation: gate-llm dialect --------------------------------------
n = P.normalize_status({"state": "waking", "in_flight": 1,
                        "last_wake_seconds": None})
check("gate dialect: waking passes through", n["state"] == "waking")
check("gate dialect: in_flight kept", n["in_flight"] == 1)
n = P.normalize_status({"state": "bogus"})
check("gate dialect: bogus state -> unknown", n["state"] == "unknown")

# -- normalisation: wakeproxy dialect --------------------------------------
n = P.normalize_status({"container_running": False, "backend_running": False})
check("wakeproxy: both down -> asleep", n["state"] == "asleep")
n = P.normalize_status({"container_running": True, "backend_running": False})
check("wakeproxy: container up, backend down -> waking",
      n["state"] == "waking")
n = P.normalize_status({"container_running": True, "backend_running": True,
                        "gateway_health": {"upstream": False}})
check("wakeproxy: upstream false -> waking", n["state"] == "waking")
n = P.normalize_status({"container_running": True, "backend_running": True,
                        "gateway_health": {"upstream": True},
                        "in_flight": 2, "last_wake_seconds": 12.5})
check("wakeproxy: upstream true -> awake", n["state"] == "awake")
check("wakeproxy: latency hint kept", n["last_wake_seconds"] == 12.5)
check("garbage payload -> unknown",
      P.normalize_status(None)["state"] == "unknown")

# -- counters over a scripted sequence --------------------------------------
now = [1000.0]
script = [
    {"container_running": False, "backend_running": False},          # asleep
    {"container_running": True, "backend_running": False},           # waking
    {"container_running": True, "backend_running": True,
     "last_wake_seconds": 9.0},                                      # awake
    {"container_running": True, "backend_running": True},            # awake
    {"container_running": False, "backend_running": False},          # asleep
]
feed = {"i": 0}


def fake_fetch(url):
    item = script[min(feed["i"], len(script) - 1)]
    return P.normalize_status(item)


GPU = {"name": "FakeGPU", "total_mib": 8192, "used_mib": 0, "free_mib": 8192,
       "util_pct": 0, "processes": []}

tmp = tempfile.mkdtemp()
cfg = {"listen_host": "127.0.0.1", "listen_port": 0, "poll_seconds": 30,
       "state_file": os.path.join(tmp, "state.json"),
       "gpu_hour_price_usd": 0.5, "gpu_hour_price_label": "test price",
       "services": [{"id": "svc", "display_name": "Svc",
                     "status_url": "http://127.0.0.1/fake",
                     "vram_mib": 4096}]}
pan = P.Panel(cfg, status_fetcher=fake_fetch, gpu_reader=lambda: GPU,
              clock=lambda: now[0])
for i in range(len(script)):
    feed["i"] = i
    pan.poll()
    now[0] += 30.0
svc = pan.state["services"]["svc"]
check("one wake counted", svc["wakes"] == 1)
check("one sleep counted", svc["sleeps"] == 1)
check("proxy latency adopted for the wake", svc["last_wake_s"] == 9.0)
check("asleep seconds accumulated", svc["asleep_s"] >= 30.0)
check("awake seconds accumulated", svc["awake_s"] >= 60.0)
snap = pan.snapshot()
check("snapshot: service awake->asleep ends asleep",
      snap["services"][0]["state"] == "asleep")
check("snapshot: counting_since set", snap["counting_since"] == 1000.0)
exp_gpu_hours = (svc["asleep_s"] / 3600.0 * 4096) / 8192
check("economics: GPU-hours math",
      abs(snap["economics"]["gpu_hours_returned"] - round(exp_gpu_hours, 3))
      < 0.01)
check("economics: avoided cost = hours x price",
      abs(snap["economics"]["avoided_cost_usd"]
          - round(exp_gpu_hours * 0.5, 2)) < 0.02)

# -- persistence: a fresh Panel on the same state file keeps counters -------
pan2 = P.Panel(cfg, status_fetcher=fake_fetch, gpu_reader=lambda: GPU,
               clock=lambda: now[0])
check("counters survive a restart",
      pan2.state["services"]["svc"]["wakes"] == 1
      and pan2.state["services"]["svc"]["asleep_s"] == svc["asleep_s"])
check("samples persisted", len(pan2.state["samples"]) == len(script))

# -- no price configured -> no cost figure ----------------------------------
cfg2 = dict(cfg, gpu_hour_price_usd=None,
            state_file=os.path.join(tmp, "state2.json"))
pan3 = P.Panel(cfg2, status_fetcher=fake_fetch, gpu_reader=lambda: GPU,
               clock=lambda: now[0])
pan3.poll()
check("no price in config -> no avoided-cost figure",
      pan3.snapshot()["economics"]["avoided_cost_usd"] is None)

print()
if FAIL:
    print(f"FAIL: {len(FAIL)} check(s) failed")
    sys.exit(1)
print("PASS")
