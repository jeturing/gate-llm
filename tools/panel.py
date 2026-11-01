#!/usr/bin/env python3
"""
panel.py -- gate-llm read-only web panel.

One Python file, standard library only, like the gatekeeper itself.
It watches the gatekeepers that guard your AI services and shows, on
one page:

  * the live state of every governed service (asleep / waking / awake,
    and since when), straight from each gatekeeper's own status
    endpoint -- reading it never wakes anything;
  * the GPU as measured, not as assumed: total / used / free VRAM and
    GPU utilisation from nvidia-smi, plus the per-process VRAM table;
  * the counters the panel itself accumulates by polling (they start
    at zero on install day and survive restarts): wakes, sleeps,
    seconds asleep / waking / awake per service, time with the whole
    GPU free, and the wake latencies observed on real transitions;
  * the economics number, computed in the open: GPU-hours returned to
    the pool = sum over services of (hours asleep x service VRAM
    footprint) / total VRAM. The avoided-cost estimate multiplies
    that by a reference price per GPU-hour taken from YOUR config,
    and the assumption is printed next to the number. No price in the
    config, no cost figure on the page.

The panel is read-only and it is not a public product surface: it
shows service display names only, never addresses. Bind it to your
internal network (the default config listens on all interfaces of a
box that is expected to live behind one; set listen_host to a
specific internal address if you prefer).

Usage:
    python3 panel.py --config /etc/gate-llm/panel.json
    python3 panel.py --config panel.json --check   # validate + one poll

Config: see panel.example.json at the repository root.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    "listen_host": "0.0.0.0",
    "listen_port": 8100,
    "poll_seconds": 30,
    "state_file": None,            # default: panel-state.json next to config
    "gpu_hour_price_usd": None,    # null -> no avoided-cost figure shown
    "gpu_hour_price_label": "",
    "services": [],
}

MAX_SAMPLES = 2880                 # 24 h at a 30 s poll
MAX_LATENCIES = 50


# ---------------------------------------------------------------------------
# status normalisation
# ---------------------------------------------------------------------------

def normalize_status(payload):
    """Reduce a gatekeeper status payload to the panel's vocabulary.

    Two dialects exist in the wild and both are supported:

    * gate-llm's own gatekeeper answers /__gate-llm/status with an
      explicit "state" of asleep / waking / awake.
    * the earlier wake-proxies answer /__wakeproxy/status with
      container_running / backend_running booleans and an optional
      gateway_health object; the state is derived from those.

    Returns {"state", "in_flight", "last_wake_seconds"}; state is one
    of asleep / waking / awake / unknown.
    """
    if not isinstance(payload, dict):
        return {"state": "unknown", "in_flight": 0, "last_wake_seconds": None}
    in_flight = payload.get("in_flight") or 0
    last_wake = payload.get("last_wake_seconds")
    if isinstance(payload.get("state"), str):
        state = payload["state"]
        if state not in ("asleep", "waking", "awake"):
            state = "unknown"
        return {"state": state, "in_flight": in_flight,
                "last_wake_seconds": last_wake}
    container = bool(payload.get("container_running"))
    backend = bool(payload.get("backend_running"))
    health = payload.get("gateway_health") or {}
    upstream_down = isinstance(health, dict) and health.get("upstream") is False
    if not container and not backend:
        state = "asleep"
    elif backend and not upstream_down:
        state = "awake"
    else:
        state = "waking"
    return {"state": state, "in_flight": in_flight,
            "last_wake_seconds": last_wake}


def fetch_status(url, timeout=4.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return normalize_status(json.loads(resp.read().decode("utf-8")))
    except Exception:
        return {"state": "unknown", "in_flight": 0, "last_wake_seconds": None}


def find_nvidia_smi():
    path = shutil.which("nvidia-smi")
    if path:
        return path
    for cand in (r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
                 "/usr/bin/nvidia-smi", "/usr/local/bin/nvidia-smi"):
        if os.path.exists(cand):
            return cand
    return None


def read_gpu(smi_path):
    """One nvidia-smi sample, or None when no GPU tool answers."""
    if not smi_path:
        return None
    try:
        out = subprocess.run(
            [smi_path, "--query-gpu=name,memory.total,memory.used,"
             "memory.free,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6)
        first = out.stdout.strip().splitlines()[0]
        name, total, used, free, util = [p.strip() for p in first.split(",")]
        procs = []
        out2 = subprocess.run(
            [smi_path, "--query-compute-apps=pid,process_name,used_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6)
        for line in out2.stdout.strip().splitlines():
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 3:
                procs.append({"pid": int(parts[0]), "name": parts[1],
                              "mib": int(float(parts[2]))})
        return {"name": name, "total_mib": int(float(total)),
                "used_mib": int(float(used)), "free_mib": int(float(free)),
                "util_pct": int(float(util)), "processes": procs}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# the panel core: polling, accounting, persistence
# ---------------------------------------------------------------------------

def _blank_service():
    return {"state": "unknown", "since": None, "wakes": 0, "sleeps": 0,
            "asleep_s": 0.0, "waking_s": 0.0, "awake_s": 0.0,
            "last_wake_s": None, "wake_latencies": [], "days": {}}


class Panel:
    def __init__(self, cfg, status_fetcher=fetch_status, gpu_reader=None,
                 clock=time.time):
        self.cfg = cfg
        self.fetch = status_fetcher
        self.clock = clock
        self.smi = find_nvidia_smi()
        self.read_gpu = gpu_reader if gpu_reader is not None else (
            lambda: read_gpu(self.smi))
        self.services_cfg = {s["id"]: s for s in cfg["services"]}
        self.state_path = cfg["state_file"]
        self.lock = threading.Lock()
        self.gpu = None
        self.last_poll = None
        self._wake_open = {}       # svc id -> {"t0": ts, "proxy_lat": seen}
        self.state = self._load()

    # -- persistence -------------------------------------------------------
    def _load(self):
        now = self.clock()
        blank = {"version": 1, "created_at": now, "all_asleep_s": 0.0,
                 "services": {}, "samples": []}
        data = blank
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if loaded.get("version") == 1:
                data = loaded
        except Exception:
            pass
        for svc_id in self.services_cfg:
            data["services"].setdefault(svc_id, _blank_service())
        return data

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh)
        os.replace(tmp, self.state_path)

    # -- one poll ------------------------------------------------------------
    def poll(self):
        now = self.clock()
        with self.lock:
            dt = 0.0
            if self.last_poll is not None:
                dt = min(now - self.last_poll, 2.0 * self.cfg["poll_seconds"])
            self.last_poll = now
            norms = {}
            for svc_id, scfg in self.services_cfg.items():
                norms[svc_id] = self.fetch(scfg["status_url"])
            self.gpu = self.read_gpu()
            # accumulate the elapsed interval under the PREVIOUS states,
            # then apply the fresh observations as transitions.
            if dt > 0:
                states_now = {sid: s["state"]
                              for sid, s in self.state["services"].items()}
                day = time.strftime("%Y-%m-%d", time.gmtime(now))
                for svc_id, svc in self.state["services"].items():
                    prev = states_now[svc_id]
                    if prev in ("asleep", "waking", "awake"):
                        svc[prev + "_s"] += dt
                        d = svc["days"].setdefault(
                            day, {"asleep_s": 0.0, "waking_s": 0.0,
                                  "awake_s": 0.0, "wakes": 0})
                        d[prev + "_s"] += dt
                if states_now and all(v == "asleep"
                                       for v in states_now.values()):
                    self.state["all_asleep_s"] += dt
            for svc_id, norm in norms.items():
                self._observe(svc_id, norm, now)
            sample = {"ts": now,
                      "vram_used": (self.gpu or {}).get("used_mib"),
                      "states": {sid: self.state["services"][sid]["state"]
                                 for sid in norms}}
            self.state["samples"].append(sample)
            self.state["samples"] = self.state["samples"][-MAX_SAMPLES:]
            self._save()

    def _observe(self, svc_id, norm, now):
        svc = self.state["services"][svc_id]
        prev, new = svc["state"], norm["state"]
        svc["in_flight"] = norm["in_flight"]
        if new == "unknown" or new == prev:
            return
        svc["state"] = new
        svc["since"] = now
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        d = svc["days"].setdefault(day, {"asleep_s": 0.0, "waking_s": 0.0,
                                         "awake_s": 0.0, "wakes": 0})
        if prev == "asleep" and new in ("waking", "awake"):
            svc["wakes"] += 1
            d["wakes"] += 1
            self._wake_open[svc_id] = {"t0": now}
        if new == "awake" and svc_id in self._wake_open:
            opened = self._wake_open.pop(svc_id)
            proxy_lat = norm.get("last_wake_seconds")
            if isinstance(proxy_lat, (int, float)) and proxy_lat > 0:
                lat = float(proxy_lat)
            else:
                lat = now - opened["t0"]
            svc["last_wake_s"] = round(lat, 1)
            svc["wake_latencies"] = (
                svc["wake_latencies"] + [round(lat, 1)])[-MAX_LATENCIES:]
        if new == "asleep" and prev in ("awake", "waking"):
            svc["sleeps"] += 1
            self._wake_open.pop(svc_id, None)

    # -- read model for the page ---------------------------------------------
    def snapshot(self):
        with self.lock:
            now = self.clock()
            gpu = self.gpu
            total_mib = (gpu or {}).get("total_mib")
            services = []
            vram_hours = 0.0
            day = time.strftime("%Y-%m-%d", time.gmtime(now))
            for svc_id, scfg in self.services_cfg.items():
                svc = self.state["services"][svc_id]
                footprint = scfg.get("vram_mib")
                if footprint:
                    vram_hours += svc["asleep_s"] / 3600.0 * footprint
                d = svc["days"].get(day, {})
                d_asleep = d.get("asleep_s", 0.0)
                d_total = (d_asleep + d.get("waking_s", 0.0)
                           + d.get("awake_s", 0.0))
                services.append({
                    "id": svc_id,
                    "display_name": scfg["display_name"],
                    "state": svc["state"],
                    "since": svc["since"],
                    "in_flight": svc.get("in_flight", 0),
                    "wakes": svc["wakes"],
                    "sleeps": svc["sleeps"],
                    "asleep_hours": round(svc["asleep_s"] / 3600.0, 2),
                    "awake_hours": round(svc["awake_s"] / 3600.0, 2),
                    "asleep_pct_today": (round(100.0 * d_asleep / d_total, 1)
                                         if d_total > 0 else None),
                    "last_wake_s": svc["last_wake_s"],
                })
            gpu_hours = None
            avoided = None
            if total_mib:
                gpu_hours = round(vram_hours / total_mib, 3)
                price = self.cfg.get("gpu_hour_price_usd")
                if isinstance(price, (int, float)) and price > 0:
                    avoided = round(gpu_hours * price, 2)
            samples = self.state["samples"]
            step = max(1, len(samples) // 720)
            return {
                "now": now,
                "counting_since": self.state["created_at"],
                "poll_seconds": self.cfg["poll_seconds"],
                "gpu": gpu,
                "services": services,
                "economics": {
                    "gpu_hours_returned": gpu_hours,
                    "all_asleep_hours": round(
                        self.state["all_asleep_s"] / 3600.0, 2),
                    "avoided_cost_usd": avoided,
                    "price_usd": self.cfg.get("gpu_hour_price_usd"),
                    "price_label": self.cfg.get("gpu_hour_price_label", ""),
                },
                "samples": samples[::step],
            }


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>gate-llm — panel</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
         background: #0d1117; color: #e6edf3; margin: 0; padding: 24px; }
  .wrap { max-width: 1080px; margin: 0 auto; }
  h1 { font-size: 22px; margin: 0 0 2px; }
  .sub { color: #8b949e; font-size: 13px; margin-bottom: 20px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
          gap: 14px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 10px;
          padding: 16px 18px; }
  .card h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .08em;
             color: #8b949e; margin: 0 0 10px; }
  .big { font-size: 34px; font-weight: 700; }
  .pill { display: inline-block; padding: 2px 10px; border-radius: 999px;
          font-size: 12px; font-weight: 600; vertical-align: middle; }
  .asleep { background: #1f3a5f; color: #9ecbff; }
  .waking { background: #4d3800; color: #ffd76a; }
  .awake  { background: #14532d; color: #7ee2a0; }
  .unknown{ background: #3a3a3a; color: #ccc; }
  .kv { display: flex; justify-content: space-between; font-size: 13px;
        padding: 3px 0; border-bottom: 1px solid #21262d; }
  .kv:last-child { border-bottom: none; }
  .bar { height: 10px; background: #21262d; border-radius: 5px; overflow: hidden;
         margin: 8px 0 4px; }
  .bar > div { height: 100%; background: #2f81f7; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  td, th { text-align: left; padding: 4px 6px; border-bottom: 1px solid #21262d; }
  .foot { color: #8b949e; font-size: 12px; margin-top: 18px; line-height: 1.5; }
  canvas { max-height: 260px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>gate-llm — panel</h1>
  <div class="sub" id="sub">loading…</div>
  <div class="grid" id="cards"></div>
  <div class="card" style="margin-top:14px">
    <h2>VRAM used — last 24 h (measured at the panel's poll)</h2>
    <canvas id="vramChart"></canvas>
  </div>
  <div class="card" style="margin-top:14px">
    <h2>Time asleep today, per service</h2>
    <canvas id="sleepChart"></canvas>
  </div>
  <div class="foot" id="foot"></div>
</div>
<script>
let vramChart = null, sleepChart = null;
const fmtH = s => s == null ? "—" : new Date(s * 1000).toLocaleTimeString();
async function refresh() {
  const r = await fetch("/api/state"); const d = await r.json();
  const g = d.gpu;
  document.getElementById("sub").textContent =
    (g ? g.name + " · " + g.total_mib.toLocaleString() + " MiB" : "no GPU tool answered") +
    " · read-only · poll every " + d.poll_seconds + " s · updated " + fmtH(d.now);
  let html = "";
  if (g) {
    const pct = Math.round(100 * g.used_mib / g.total_mib);
    html += `<div class="card"><h2>GPU memory</h2>
      <div class="big">${g.used_mib.toLocaleString()} MiB in use</div>
      <div class="bar"><div style="width:${pct}%"></div></div>
      <div class="kv"><span>Free</span><b>${g.free_mib.toLocaleString()} MiB</b></div>
      <div class="kv"><span>GPU utilisation</span><b>${g.util_pct} %</b></div>
      <table><tr><th>process</th><th>VRAM</th></tr>${
        g.processes.map(p => `<tr><td>${p.name}</td><td>${p.mib.toLocaleString()} MiB</td></tr>`).join("")
        || '<tr><td colspan="2">no compute processes — the GPU is empty</td></tr>'}
      </table></div>`;
  }
  for (const s of d.services) {
    const label = s.state === "awake" && s.in_flight > 0 ? "serving" : s.state;
    html += `<div class="card"><h2>${s.display_name}</h2>
      <div><span class="pill ${s.state}">${label}</span>
      <span style="color:#8b949e;font-size:13px"> since ${fmtH(s.since)}</span></div>
      <div style="margin-top:10px">
      <div class="kv"><span>Requests in flight</span><b>${s.in_flight}</b></div>
      <div class="kv"><span>Wakes counted</span><b>${s.wakes}</b></div>
      <div class="kv"><span>Last wake took</span><b>${s.last_wake_s == null ? "—" : s.last_wake_s + " s"}</b></div>
      <div class="kv"><span>Asleep (total)</span><b>${s.asleep_hours} h</b></div>
      <div class="kv"><span>Asleep today</span><b>${s.asleep_pct_today == null ? "—" : s.asleep_pct_today + " %"}</b></div>
      </div></div>`;
  }
  const e = d.economics;
  html += `<div class="card"><h2>Economics — measured, assumptions printed</h2>
    <div class="big">${e.gpu_hours_returned == null ? "—" : e.gpu_hours_returned} GPU-hours returned</div>
    <div class="kv"><span>Whole GPU free (all services asleep)</span><b>${e.all_asleep_hours} h</b></div>${
    e.avoided_cost_usd == null ? "" :
    `<div class="kv"><span>Avoided cost, estimate</span><b>US$ ${e.avoided_cost_usd}</b></div>
     <div style="font-size:12px;color:#8b949e;margin-top:6px">Assumption: ${e.price_label || (e.price_usd + " US$/GPU-hour")} — GPU-hours returned × US$ ${e.price_usd}/GPU-hour. Edit the price in the panel config to use your own.</div>`}
    </div>`;
  document.getElementById("cards").innerHTML = html;
  document.getElementById("foot").textContent =
    "Counters are the panel's own, accumulated by polling the gatekeepers' status endpoints; " +
    "they read zero from the day the panel was installed (" +
    new Date(d.counting_since * 1000).toLocaleDateString() + ") and are not back-filled. " +
    "Reading this page wakes nothing.";
  if (window.Chart) {
    const pts = d.samples.filter(p => p.vram_used != null);
    if (vramChart) vramChart.destroy();
    vramChart = new Chart(document.getElementById("vramChart"), {
      type: "line",
      data: { datasets: [{ label: "VRAM used (MiB)", stepped: true,
        borderColor: "#2f81f7", backgroundColor: "rgba(47,129,247,.15)",
        fill: true, pointRadius: 0,
        data: pts.map(p => ({ x: p.ts * 1000, y: p.vram_used })) }] },
      options: { animation: false, scales: {
        x: { type: "linear", ticks: { color: "#8b949e",
             callback: v => new Date(v).toLocaleTimeString() } },
        y: { ticks: { color: "#8b949e" }, beginAtZero: true } },
        plugins: { legend: { labels: { color: "#e6edf3" } } } }
    });
    if (sleepChart) sleepChart.destroy();
    sleepChart = new Chart(document.getElementById("sleepChart"), {
      type: "bar",
      data: { labels: d.services.map(s => s.display_name),
        datasets: [{ label: "% of today asleep", backgroundColor: "#1f6feb",
          data: d.services.map(s => s.asleep_pct_today || 0) }] },
      options: { indexAxis: "y", animation: false,
        scales: { x: { min: 0, max: 100, ticks: { color: "#8b949e" } },
                  y: { ticks: { color: "#e6edf3" } } },
        plugins: { legend: { labels: { color: "#e6edf3" } } } }
    });
  }
}
refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>
"""


def make_handler(panel):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/?"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._send(200, json.dumps(panel.snapshot()),
                           "application/json")
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):
            self._send(405, "read-only", "text/plain")

    return Handler


def load_config(path):
    cfg = dict(DEFAULTS)
    with open(path, "r", encoding="utf-8") as fh:
        user = json.load(fh)
    cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
    if not cfg["state_file"]:
        cfg["state_file"] = os.path.join(
            os.path.dirname(os.path.abspath(path)), "panel-state.json")
    if not cfg["services"]:
        raise SystemExit("panel config declares no services")
    for svc in cfg["services"]:
        for key in ("id", "display_name", "status_url"):
            if key not in svc:
                raise SystemExit(f"service entry missing {key}: {svc}")
    return cfg


def main(argv=None):
    ap = argparse.ArgumentParser(description="gate-llm read-only panel")
    ap.add_argument("--config", required=True)
    ap.add_argument("--check", action="store_true",
                    help="validate config, poll once, print the snapshot")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    panel = Panel(cfg)
    panel.poll()
    if args.check:
        print(json.dumps(panel.snapshot(), indent=2))
        return 0
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            stop.wait(cfg["poll_seconds"])
            try:
                panel.poll()
            except Exception as exc:            # a bad poll must not kill us
                print(f"[panel] poll failed: {exc}", flush=True)

    threading.Thread(target=loop, daemon=True).start()
    server = ThreadingHTTPServer((cfg["listen_host"], cfg["listen_port"]),
                                 make_handler(panel))
    print(f"[panel] listening on {cfg['listen_host']}:{cfg['listen_port']} "
          f"for {len(cfg['services'])} service(s); state at "
          f"{cfg['state_file']}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
