#!/usr/bin/env python3
"""
gate-llm -- on-demand wake proxy for AI services sharing one small GPU.

One lightweight gatekeeper process owns the public port of each registered
service. The real backend (an LLM server, a TTS engine, an STT service...)
sleeps stopped, holding no VRAM. The first request that arrives wakes it:
the gatekeeper starts the backend, waits until its health endpoint reports
ready, and only then forwards the request -- the caller waits a few seconds
instead of getting an error. After a configurable idle window with no
request in flight, the gatekeeper stops the backend again and the GPU
memory is released for whatever needs it next.

Pure Python 3 standard library. No dependencies.

Design notes
------------
* Holding, not failing: a cold request is *held* while the backend wakes.
  Callers therefore need a client timeout longer than the wake time of the
  service (tens of seconds for a local model load). This is the central
  trade-off of the pattern and it is deliberate.
* Health-gated: a backend is "awake" only when its health URL answers with
  an expected status. A process that started but is still loading its model
  does not receive traffic.
* Safe stop: the backend is stopped only when zero requests are in flight.
  A long generation is never cut by the idle timer.
* One source of truth: services are declared in a single JSON file. The
  companion menu (tools/menu.py) edits the same file; the gatekeeper picks
  up runtime changes (idle window, active model) without a restart.

Start/stop contract
-------------------
Each service (or each model of a service) declares `start` and `stop`
command arrays, e.g. ["docker", "start", "my-llm"] / ["docker", "stop",
"-t", "30", "my-llm"], or ["systemctl", "start", "my-llm.service"] /
["systemctl", "stop", "my-llm.service"]. If `stop` is omitted, `start` is
spawned as a long-running child process and stopping means terminating its
process group (SIGTERM, grace, SIGKILL).

Built-in endpoints on every service port (they never count as activity):
    GET  /__gate-llm/status   JSON state, does not wake the backend
    POST /__gate-llm/wake     wake now and wait until healthy
    POST /__gate-llm/sleep    stop now (409 while a request is in flight)

Usage:
    python3 gate_llm_proxy.py --config /etc/gate-llm/services.json
    python3 gate_llm_proxy.py --config services.json --check
"""

import argparse
import http.client
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

STATUS_PREFIX = "/__gate-llm"
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}


def log(service, msg):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] [gate-llm:{service}] {msg}", file=sys.stderr, flush=True)


class WakeError(Exception):
    pass


def validate_config(cfg):
    """Return a list of human-readable problems (empty = valid)."""
    problems = []
    services = cfg.get("services")
    if not isinstance(services, list) or not services:
        return ["config must contain a non-empty 'services' list"]
    names, listen_ports = set(), set()
    for svc in services:
        name = svc.get("name")
        if not name:
            problems.append("every service needs a 'name'")
            continue
        if name in names:
            problems.append(f"duplicate service name: {name}")
        names.add(name)
        for field in ("listen_port", "backend_port"):
            if not isinstance(svc.get(field), int):
                problems.append(f"service {name}: '{field}' must be an integer port")
        lp = svc.get("listen_port")
        if lp in listen_ports:
            problems.append(f"duplicate listen_port: {lp}")
        listen_ports.add(lp)
        models = svc.get("models") or []
        ids = set()
        for model in models:
            mid = model.get("id")
            if not mid:
                problems.append(f"service {name}: every model needs an 'id'")
            if mid in ids:
                problems.append(f"service {name}: duplicate model id {mid}")
            ids.add(mid)
            if not model.get("start"):
                problems.append(f"service {name} model {mid}: missing 'start' command")
        if models:
            active = svc.get("active_model")
            if active not in ids:
                problems.append(
                    f"service {name}: 'active_model' ({active!r}) is not one of its models")
        elif not svc.get("start"):
            problems.append(f"service {name}: missing 'start' command (and no models)")
    return problems


class Service:
    """State machine for one registered backend: asleep -> waking -> awake."""

    def __init__(self, cfg, config_path):
        self.cfg = cfg
        self.config_path = config_path
        self.name = cfg["name"]
        self.listen_host = cfg.get("listen_host", "0.0.0.0")
        self.listen_port = cfg["listen_port"]
        self.backend_host = cfg.get("backend_host", "127.0.0.1")
        self.backend_port = cfg["backend_port"]
        self.health_url = cfg.get(
            "health_url", f"http://{self.backend_host}:{self.backend_port}/health")
        self.health_expect = cfg.get("health_expect_status", [200])
        self.health_timeout_s = float(cfg.get("health_timeout_s", 300))
        self.health_poll_s = float(cfg.get("health_poll_s", 0.5))
        self.idle_timeout_s = float(cfg.get("idle_timeout_s", 1200))
        self.stop_grace_s = float(cfg.get("stop_grace_s", 30))
        self.state = "asleep"
        self.in_flight = 0
        self.last_activity = 0.0
        self.awake_since = None
        self.last_wake_seconds = None
        self._proc = None              # child process when start was spawned directly
        self._wake_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._cfg_mtime = self._mtime()

    # -- config ---------------------------------------------------------
    def _mtime(self):
        try:
            return os.path.getmtime(self.config_path)
        except OSError:
            return 0.0

    def reload_if_changed(self):
        """Pick up runtime edits (idle window, active model, health) live.

        Structural changes (ports, added/removed services) need a restart of
        the gatekeeper; they are ignored here on purpose.
        """
        mtime = self._mtime()
        if mtime == self._cfg_mtime:
            return
        try:
            with open(self.config_path, "r", encoding="utf-8") as fh:
                fresh = json.load(fh)
            for svc in fresh.get("services", []):
                if svc.get("name") == self.name:
                    # Scalar tuning always applies live. The start/stop pair
                    # and the active model only apply while asleep: applying
                    # them to a running backend could stop the wrong process.
                    keys = ["idle_timeout_s", "stop_grace_s", "health_timeout_s",
                            "health_url", "health_expect_status"]
                    if self.state == "asleep":
                        keys += ["active_model", "start", "stop", "models"]
                    for key in keys:
                        if key in svc:
                            self.cfg[key] = svc[key]
                    self.idle_timeout_s = float(self.cfg.get("idle_timeout_s", 1200))
                    self.stop_grace_s = float(self.cfg.get("stop_grace_s", 30))
                    self.health_timeout_s = float(self.cfg.get("health_timeout_s", 300))
                    self.health_url = self.cfg.get("health_url", self.health_url)
                    self.health_expect = self.cfg.get("health_expect_status", [200])
                    log(self.name, "configuration reloaded")
                    break
        except (OSError, ValueError) as exc:
            log(self.name, f"config reload failed (keeping previous): {exc}")
        finally:
            self._cfg_mtime = mtime

    def _effective(self):
        """(start, stop) for the active model, or the service-level pair."""
        models = self.cfg.get("models") or []
        if models:
            active = self.cfg.get("active_model")
            for model in models:
                if model.get("id") == active:
                    return model.get("start"), model.get("stop")
            raise WakeError(f"active model {active!r} not found")
        return self.cfg.get("start"), self.cfg.get("stop")

    # -- health ---------------------------------------------------------
    def health_ok(self, timeout=0.6):
        req = urllib.request.Request(self.health_url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status in self.health_expect
        except urllib.error.HTTPError as exc:      # e.g. 503 while loading
            return exc.code in self.health_expect
        except Exception:
            return False

    # -- lifecycle ------------------------------------------------------
    def ensure_awake(self):
        """Start the backend if needed and wait until healthy.

        Returns the seconds the caller waited (0.0 if already awake).
        Raises WakeError if the backend does not become healthy in time.
        Concurrent callers share a single wake: they wait on the same lock.
        """
        self.reload_if_changed()
        if self.state == "awake" and self.health_ok():
            return 0.0
        with self._wake_lock:
            if self.state == "awake" and self.health_ok():
                return 0.0
            started = time.time()
            self.state = "waking"
            start_cmd, _ = self._effective()
            log(self.name, f"waking backend: {' '.join(start_cmd)}")
            self._run_start(start_cmd)
            deadline = started + self.health_timeout_s
            while time.time() < deadline:
                if self.health_ok(timeout=2.0):
                    self.state = "awake"
                    self.awake_since = time.time()
                    self.last_activity = time.time()
                    self.last_wake_seconds = round(time.time() - started, 2)
                    log(self.name, f"backend healthy after {self.last_wake_seconds}s")
                    return self.last_wake_seconds
                time.sleep(self.health_poll_s)
            self.state = "asleep"
            raise WakeError(
                f"backend did not become healthy within {self.health_timeout_s:.0f}s")

    def _run_start(self, start_cmd):
        _, stop_cmd = self._effective()
        if stop_cmd:
            # Daemon-style start (docker start, systemctl start, a script):
            # the command returns once the backend is launched.
            subprocess.run(start_cmd, check=True, timeout=120,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._proc = None
        else:
            # Direct child: we own the process and stop it by process group.
            self._proc = subprocess.Popen(
                start_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)

    def stop_backend(self):
        """Stop the backend now. Caller must guarantee no in-flight work."""
        with self._wake_lock:
            if self.state == "asleep":
                return True
            self.state = "stopping"
            _, stop_cmd = self._effective()
            try:
                if stop_cmd:
                    subprocess.run(stop_cmd, timeout=self.stop_grace_s + 60,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                elif self._proc is not None:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
                    try:
                        self._proc.wait(timeout=self.stop_grace_s)
                    except subprocess.TimeoutExpired:
                        os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                        self._proc.wait(timeout=10)
            except Exception as exc:  # stopping must never crash the gatekeeper
                log(self.name, f"error while stopping backend: {exc}")
            self._proc = None
            self.state = "asleep"
            self.awake_since = None
            log(self.name, "backend stopped; GPU memory released")
            return True

    def request_sleep(self):
        with self._state_lock:
            busy = self.in_flight > 0
        if busy:
            return False
        return self.stop_backend()

    # -- accounting -------------------------------------------------------
    def begin_request(self):
        with self._state_lock:
            self.in_flight += 1
            self.last_activity = time.time()

    def end_request(self):
        with self._state_lock:
            self.in_flight -= 1
            self.last_activity = time.time()

    def status(self):
        return {
            "service": self.name,
            "state": self.state,
            "in_flight": self.in_flight,
            "idle_timeout_s": self.idle_timeout_s,
            "active_model": self.cfg.get("active_model"),
            "awake_since": self.awake_since,
            "last_wake_seconds": self.last_wake_seconds,
            "listen": f"{self.listen_host}:{self.listen_port}",
            "backend": f"{self.backend_host}:{self.backend_port}",
        }

    # -- idle reaper ------------------------------------------------------
    def reaper(self):
        while True:
            time.sleep(2.0)
            self.reload_if_changed()
            if (self.state == "awake" and self.idle_timeout_s > 0
                    and self.in_flight == 0 and self.last_activity
                    and time.time() - self.last_activity > self.idle_timeout_s):
                log(self.name,
                    f"idle for more than {self.idle_timeout_s:.0f}s; stopping backend")
                self.stop_backend()


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def read_request_body(handler):
    """Read a request body framed by Content-Length or chunked encoding."""
    length = handler.headers.get("Content-Length")
    if length is not None:
        return handler.rfile.read(int(length))
    if "chunked" in (handler.headers.get("Transfer-Encoding") or "").lower():
        chunks = []
        while True:
            size_line = handler.rfile.readline(65536).strip()
            size = int(size_line.split(b";")[0], 16)
            if size == 0:
                while True:  # consume trailers up to the blank line
                    line = handler.rfile.readline(65536)
                    if line in (b"\r\n", b"\n", b""):
                        break
                break
            chunks.append(handler.rfile.read(size))
            handler.rfile.read(2)  # trailing CRLF
        return b"".join(chunks)
    return None


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "gate-llm"
        sys_version = ""

        def log_message(self, *args):  # keep stderr for gatekeeper events
            pass

        # -- helpers -----------------------------------------------------
        def _send_json(self, code, payload):
            body = json.dumps(payload, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        # -- routing -----------------------------------------------------
        def _handle(self):
            path = urlsplit(self.path).path
            if path == STATUS_PREFIX + "/status" and self.command == "GET":
                self._send_json(200, service.status())
                return
            if path == STATUS_PREFIX + "/wake" and self.command == "POST":
                read_request_body(self)  # drain, ignore
                try:
                    waited = service.ensure_awake()
                    payload = service.status()
                    payload["wake_seconds"] = waited
                    self._send_json(200, payload)
                except WakeError as exc:
                    self._send_json(502, {"error": str(exc), "service": service.name})
                return
            if path == STATUS_PREFIX + "/sleep" and self.command == "POST":
                read_request_body(self)
                if service.request_sleep():
                    self._send_json(200, service.status())
                else:
                    self._send_json(409, {
                        "error": "requests in flight; refusing to stop mid-work",
                        "service": service.name})
                return
            self._proxy()

        def _proxy(self):
            service.begin_request()
            try:
                service.ensure_awake()      # the cold request WAITS here
                body = read_request_body(self)
                headers = {}
                for key, value in self.headers.items():
                    if key.lower() in HOP_BY_HOP or key.lower() == "host":
                        continue
                    headers[key] = value
                headers["Host"] = f"{service.backend_host}:{service.backend_port}"
                headers["X-Forwarded-For"] = self.client_address[0]
                headers["X-Forwarded-Proto"] = "http"
                conn = http.client.HTTPConnection(
                    service.backend_host, service.backend_port, timeout=7200)
                conn.request(self.command, self.path, body=body, headers=headers)
                resp = conn.getresponse()
                self.send_response(resp.status, resp.reason)
                length = None
                for key, value in resp.getheaders():
                    low = key.lower()
                    if low in HOP_BY_HOP:
                        continue
                    if low == "content-length":
                        length = int(value)
                        continue
                    self.send_header(key, value)
                if length is not None:
                    self.send_header("Content-Length", str(length))
                else:
                    self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while True:                 # stream, flushing per chunk (SSE-safe)
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    if self.command == "HEAD":
                        continue
                    if length is not None:
                        self.wfile.write(chunk)
                    else:
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.flush()
                if length is None and self.command != "HEAD":
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                conn.close()
            except WakeError as exc:
                try:
                    self._send_json(502, {"error": str(exc), "service": service.name})
                except Exception:
                    pass
            except (BrokenPipeError, ConnectionResetError):
                pass  # caller went away mid-stream; nothing to repair
            except Exception as exc:
                log(service.name, f"proxy error: {exc}")
                try:
                    self._send_json(502, {"error": "backend unreachable",
                                          "service": service.name})
                except Exception:
                    pass
            finally:
                service.end_request()

        do_GET = _handle
        do_POST = _handle
        do_PUT = _handle
        do_PATCH = _handle
        do_DELETE = _handle
        do_HEAD = _handle
        do_OPTIONS = _handle

    return Handler


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="gate-llm",
        description="On-demand wake proxy for AI services on a single GPU.")
    parser.add_argument(
        "--config",
        default=os.environ.get("GATE_LLM_CONFIG") or (
            "/etc/gate-llm/services.json" if os.path.exists("/etc/gate-llm/services.json")
            else "services.json"),
        help="path to the services JSON file")
    parser.add_argument("--check", action="store_true",
                        help="validate the config and exit")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    problems = validate_config(cfg)
    if problems:
        for problem in problems:
            print(f"config error: {problem}", file=sys.stderr)
        return 1
    if args.check:
        names = ", ".join(s["name"] for s in cfg["services"])
        print(f"config OK: {len(cfg['services'])} service(s): {names}")
        return 0

    servers = []
    for svc_cfg in cfg["services"]:
        service = Service(svc_cfg, args.config)
        server = ThreadingHTTPServer(
            (service.listen_host, service.listen_port), make_handler(service))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        threading.Thread(target=service.reaper, daemon=True).start()
        servers.append(server)
        log(service.name,
            f"gatekeeper listening on {service.listen_host}:{service.listen_port} "
            f"-> backend {service.backend_host}:{service.backend_port} "
            f"(idle window {service.idle_timeout_s:.0f}s)")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
