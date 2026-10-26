#!/usr/bin/env python3
"""
menu.py -- terminal manager for gate-llm.

A dependency-free interactive menu that administers the SAME services.json
the gatekeeper reads -- one source of truth:

  * list services/models with live state (asleep / awake) and VRAM usage
  * choose which model a service serves (the change is applied by sleeping
    the service and waking it with the newly selected model)
  * register a new service, or a new model for an existing service
  * remove a service or a model
  * wake / sleep a service by hand
  * tune the idle window per service

Every save is validated first (schema, duplicate names/ports, a bind probe
on new listen ports, and a health check when the service is awake) and is
written atomically, keeping a .bak of the previous config.

Runtime edits (idle window, active model) are picked up by the running
gatekeeper within seconds. Structural edits (add/remove, ports) need a
gatekeeper restart; the menu says so and can run it for you.

    python3 tools/menu.py [--config /etc/gate-llm/services.json]
"""

import argparse
import json
import os
import shlex
import socket
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
try:
    from gate_llm_proxy import validate_config
except Exception:  # fallback: minimal local validation if the proxy is absent
    def validate_config(cfg):
        problems = []
        if not isinstance(cfg.get("services"), list) or not cfg["services"]:
            problems.append("config must contain a non-empty 'services' list")
        return problems

STATUS = "/__gate-llm/status"
WAKE = "/__gate-llm/wake"
SLEEP = "/__gate-llm/sleep"


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def ask(prompt, default=None, required=True):
    suffix = f" [{default}]" if default is not None else ""
    while True:
        try:
            value = input(f"{prompt}{suffix}: ").strip()
        except EOFError:
            raise KeyboardInterrupt
        if value:
            return value
        if default is not None:
            return str(default)
        if not required:
            return ""
        print("  (required)")


def ask_int(prompt, default=None, minimum=None, maximum=None):
    while True:
        raw = ask(prompt, default)
        try:
            value = int(raw)
        except ValueError:
            print("  please enter an integer")
            continue
        if minimum is not None and value < minimum:
            print(f"  must be >= {minimum}")
            continue
        if maximum is not None and value > maximum:
            print(f"  must be <= {maximum}")
            continue
        return value


def confirm(prompt, default=False):
    hint = "Y/n" if default else "y/N"
    try:
        value = input(f"{prompt} [{hint}]: ").strip().lower()
    except EOFError:
        raise KeyboardInterrupt
    if not value:
        return default
    return value in ("y", "yes", "s", "si")


def http_json(url, method="GET", timeout=3.0):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def gate_base(service):
    host = service.get("listen_host", "0.0.0.0")
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{service['listen_port']}"


def service_status(service):
    try:
        return http_json(gate_base(service) + STATUS, timeout=1.5)
    except Exception:
        return None


def run_quiet(argv, timeout=4):
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return out.stdout.strip()
    except Exception:
        return ""


def gpu_totals():
    out = run_quiet(["nvidia-smi", "--query-gpu=memory.free,memory.total",
                     "--format=csv,noheader,nounits"])
    if not out:
        return None
    try:
        free, total = out.splitlines()[0].split(",")
        return int(free.strip()), int(total.strip())
    except (ValueError, IndexError):
        return None


def container_name(service):
    """Best effort: the docker container behind the active start command."""
    arrays = []
    models = service.get("models") or []
    if models:
        for model in models:
            if model.get("id") == service.get("active_model"):
                arrays = [model.get("start") or [], model.get("stop") or []]
    else:
        arrays = [service.get("start") or [], service.get("stop") or []]
    for argv in arrays:
        if len(argv) >= 3 and argv[0] == "docker" and argv[1] in ("start", "stop"):
            for token in argv[2:]:
                if not token.startswith("-") and token not in ("-t",):
                    if token.isdigit():
                        continue
                    return token
    return None


def service_vram(service):
    name = container_name(service)
    if not name:
        return None
    pid = run_quiet(["docker", "inspect", "-f", "{{.State.Pid}}", name])
    if not pid or pid in ("0", "<no value>"):
        return 0
    out = run_quiet(["nvidia-smi", "--query-compute-apps=pid,used_memory",
                     "--format=csv,noheader,nounits"])
    total = 0
    found = False
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2 and parts[0] == pid:
            total += int(parts[1])
            found = True
    return total if found else 0


def port_is_free(port, host="0.0.0.0"):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def health_reachable(service):
    url = service.get("health_url")
    if not url:
        url = f"http://{service.get('backend_host','127.0.0.1')}:{service['backend_port']}/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status in service.get("health_expect_status", [200])
    except urllib.error.HTTPError as exc:
        return exc.code in service.get("health_expect_status", [200])
    except Exception:
        return False


# ---------------------------------------------------------------------------
# config handling
# ---------------------------------------------------------------------------

class Config:
    def __init__(self, path):
        self.path = path
        with open(path, "r", encoding="utf-8") as fh:
            self.data = json.load(fh)

    @property
    def services(self):
        return self.data["services"]

    def find(self, name):
        for svc in self.services:
            if svc["name"] == name:
                return svc
        return None

    def save(self):
        problems = validate_config(self.data)
        if problems:
            for problem in problems:
                print(f"  config error: {problem}")
            print("Not saved.")
            return False
        backup = self.path + ".bak"
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                previous = fh.read()
            with open(backup, "w", encoding="utf-8") as fh:
                fh.write(previous)
        except OSError:
            pass
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, self.path)
        print(f"Saved {self.path} (backup at {backup}).")
        return True


def pick_service(cfg, prompt="Service number"):
    show_table(cfg)
    idx = ask_int(prompt, minimum=1, maximum=len(cfg.services))
    return cfg.services[idx - 1]


def show_table(cfg):
    totals = gpu_totals()
    print()
    if totals:
        print(f"GPU: {totals[0]:,} MiB free of {totals[1]:,} MiB total")
    else:
        print("GPU: nvidia-smi not available on this host")
    print(f"{'#':<3} {'service':<12} {'state':<9} {'active model':<22} {'idle':<7} VRAM")
    print("-" * 78)
    for i, svc in enumerate(cfg.services, 1):
        status = service_status(svc)
        state = status["state"] if status else "unknown"
        model = svc.get("active_model") or "-"
        idle = svc.get("idle_timeout_s", 1200)
        idle_txt = "never" if not idle else f"{int(idle // 60)}m"
        vram = service_vram(svc)
        vram_txt = f"{vram:,} MiB" if vram else "-"
        print(f"{i:<3} {svc['name']:<12} {state:<9} {model:<22} {idle_txt:<7} {vram_txt}")
        if status is None:
            print("      (gatekeeper not responding on its port; state unknown)")
    print()


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------

def gate_post(service, path, timeout=30.0):
    try:
        return http_json(gate_base(service) + path, method="POST", timeout=timeout)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            payload = {"error": f"HTTP {exc.code}"}
        return payload


def do_wake(cfg):
    svc = pick_service(cfg, "Wake which service?")
    timeout = float(svc.get("health_timeout_s", 300)) + 30
    print(f"Waking {svc['name']} (cold wake can take a while)...")
    result = gate_post(svc, WAKE, timeout=timeout)
    if "error" in result:
        print(f"  wake failed: {result['error']}")
    else:
        print(f"  {svc['name']} is {result.get('state')} "
              f"(waited {result.get('wake_seconds')}s)")


def do_sleep(cfg):
    svc = pick_service(cfg, "Sleep which service?")
    result = gate_post(svc, SLEEP)
    if "error" in result:
        print(f"  not stopped: {result['error']}")
    else:
        print(f"  {svc['name']} is asleep; its GPU memory is released.")


def do_switch_model(cfg):
    svc = pick_service(cfg, "Switch model of which service?")
    models = svc.get("models") or []
    if not models:
        print(f"  {svc['name']} has a single fixed start/stop pair (no model list).")
        print("  Add a model first (option 6) to make it switchable.")
        return
    print(f"Models of {svc['name']} (active: {svc.get('active_model')}):")
    for i, model in enumerate(models, 1):
        mark = " *" if model["id"] == svc.get("active_model") else ""
        print(f"  {i}. {model['id']} -- {model.get('label', '')}{mark}")
    idx = ask_int("Model number", minimum=1, maximum=len(models))
    chosen = models[idx - 1]
    if chosen["id"] == svc.get("active_model"):
        print("  That model is already active.")
        return
    status = service_status(svc)
    awake = bool(status and status.get("state") == "awake")
    if awake and not health_reachable(svc):
        print("  CAUTION: the gatekeeper reports the service awake but its "
              "health endpoint does not answer.")
        if not confirm("  Continue anyway?", default=False):
            return
    if awake:
        # Stop FIRST, while the gatekeeper still runs the old config: the
        # stop then targets the backend that is actually running.
        print(f"  {svc['name']} is awake; stopping it before the switch...")
        result = gate_post(svc, SLEEP)
        if "error" in result:
            print(f"  cannot switch now: {result['error']}")
            return
    svc["active_model"] = chosen["id"]
    if not cfg.save():
        return
    print(f"  active model of {svc['name']} is now {chosen['id']}.")
    if awake and confirm(f"  Wake {svc['name']} with the new model now?", default=True):
        result = gate_post(svc, WAKE,
                           timeout=float(svc.get("health_timeout_s", 300)) + 30)
        if "error" in result:
            print(f"  wake failed: {result['error']}")
        else:
            print(f"  awake with {chosen['id']} (waited {result.get('wake_seconds')}s)")


def ask_start_stop(existing=None):
    """Prompt for a start/stop pair. Returns (start, stop|None)."""
    print("  How is it run?")
    print("    1. Docker container (created beforehand with --restart no)")
    print("    2. Command (a start line; optional stop line)")
    mode = ask_int("  Mode", default=1, minimum=1, maximum=2)
    if mode == 1:
        container = ask("  Container name")
        return (["docker", "start", container],
                ["docker", "stop", "-t", "30", container])
    start_line = ask("  Start command")
    stop_line = ask("  Stop command (blank = gatekeeper owns the process)",
                    required=False)
    return shlex.split(start_line), (shlex.split(stop_line) if stop_line else None)


def do_add_service(cfg):
    name = ask("Service name (short, no spaces)")
    if cfg.find(name):
        print(f"  a service named {name} already exists; aborted.")
        return
    listen_port = ask_int("Public (gatekeeper) port", minimum=1, maximum=65535)
    if any(s["listen_port"] == listen_port for s in cfg.services):
        print("  that listen port is already used by another service; aborted.")
        return
    if not port_is_free(listen_port):
        print(f"  port {listen_port} is already bound on this host; aborted.")
        return
    backend_port = ask_int("Backend port (loopback)", minimum=1, maximum=65535)
    if any(s["backend_port"] == backend_port for s in cfg.services):
        print("  that backend port is already used by another service; aborted.")
        return
    health_path = ask("Health path", default="/health")
    idle_min = ask_int("Idle window in minutes (0 = never sleep)", default=20, minimum=0)
    start, stop = ask_start_stop()
    svc = {
        "name": name,
        "description": ask("Description (optional)", required=False),
        "listen_host": "0.0.0.0",
        "listen_port": listen_port,
        "backend_host": "127.0.0.1",
        "backend_port": backend_port,
        "health_url": f"http://127.0.0.1:{backend_port}{health_path}",
        "health_expect_status": [200],
        "health_timeout_s": 300,
        "idle_timeout_s": idle_min * 60,
        "stop_grace_s": 30,
        "start": start,
    }
    if stop:
        svc["stop"] = stop
    cfg.services.append(svc)
    if cfg.save():
        print("  Structural change: restart the gatekeeper to open the new port.")
        maybe_restart_gatekeeper()


def do_add_model(cfg):
    svc = pick_service(cfg, "Add a model to which service?")
    models = svc.get("models")
    if not models:
        base_id = ask("Model id for the CURRENT start/stop pair", default="main")
        first = {"id": base_id, "label": ask("Label for it", required=False)}
        first["start"] = svc.pop("start")
        if svc.get("stop"):
            first["stop"] = svc.pop("stop")
        svc["models"] = [first]
        svc["active_model"] = base_id
        models = svc["models"]
        print(f"  converted: current pair is now model '{base_id}' (still active).")
    model_id = ask("New model id")
    if any(m["id"] == model_id for m in models):
        print("  a model with that id already exists; aborted.")
        return
    label = ask("Label (shown in this menu)", required=False)
    start, stop = ask_start_stop()
    model = {"id": model_id, "label": label, "start": start}
    if stop:
        model["stop"] = stop
    models.append(model)
    if cfg.save():
        print(f"  model '{model_id}' registered for {svc['name']} "
              f"(use option 3 to make it active).")


def do_remove_service(cfg):
    svc = pick_service(cfg, "Remove which service?")
    status = service_status(svc)
    if status and status.get("state") == "awake":
        result = gate_post(svc, SLEEP)
        if "error" in result:
            print(f"  it is awake and busy ({result['error']}); aborted.")
            return
        print("  it was awake; stopped first.")
    if not confirm(f"Remove service {svc['name']} from the config?", default=False):
        return
    cfg.services.remove(svc)
    if cfg.save():
        print("  Structural change: restart the gatekeeper to apply.")
        maybe_restart_gatekeeper()


def do_remove_model(cfg):
    svc = pick_service(cfg, "Remove a model from which service?")
    models = svc.get("models") or []
    if len(models) < 2:
        print("  nothing removable: a service needs at least one model.")
        return
    removable = [m for m in models if m["id"] != svc.get("active_model")]
    for i, model in enumerate(removable, 1):
        print(f"  {i}. {model['id']} -- {model.get('label', '')}")
    idx = ask_int("Model number", minimum=1, maximum=len(removable))
    victim = removable[idx - 1]
    if not confirm(f"Remove model {victim['id']} from {svc['name']}?", default=False):
        return
    models.remove(victim)
    cfg.save()


def do_set_idle(cfg):
    svc = pick_service(cfg, "Idle window of which service?")
    current = int(float(svc.get("idle_timeout_s", 1200)) // 60)
    minutes = ask_int("Idle window in minutes (0 = never sleep)",
                      default=current, minimum=0)
    svc["idle_timeout_s"] = minutes * 60
    if cfg.save():
        print("  The running gatekeeper picks this up within seconds; "
              "no restart needed.")
        status = service_status(svc)
        if status and status.get("state") == "awake" and not health_reachable(svc):
            print("  CAUTION: service is awake but its health endpoint "
                  "does not answer right now.")


def maybe_restart_gatekeeper():
    if not confirm("Restart the gatekeeper service now (systemctl)?", default=False):
        print("  Apply later with: sudo systemctl restart gate-llm")
        return
    result = subprocess.run(["systemctl", "restart", "gate-llm"])
    if result.returncode != 0:
        print("  systemctl failed (permissions?). Run: sudo systemctl restart gate-llm")


MENU = """
gate-llm -- menu
  1. Wake a service
  2. Sleep a service
  3. Switch the active model of a service
  4. Set the idle window of a service
  5. Add a service
  6. Add a model to a service
  7. Remove a service
  8. Remove a model
  0. Exit"""


def main(argv=None):
    parser = argparse.ArgumentParser(prog="menu.py",
                                     description="Terminal manager for gate-llm.")
    parser.add_argument(
        "--config",
        default=os.environ.get("GATE_LLM_CONFIG") or (
            "/etc/gate-llm/services.json" if os.path.exists("/etc/gate-llm/services.json")
            else "services.json"))
    args = parser.parse_args(argv)
    cfg = Config(args.config)
    actions = {"1": do_wake, "2": do_sleep, "3": do_switch_model,
               "4": do_set_idle, "5": do_add_service, "6": do_add_model,
               "7": do_remove_service, "8": do_remove_model}
    try:
        while True:
            show_table(cfg)
            print(MENU)
            choice = input("Option: ").strip()
            if choice == "0":
                break
            action = actions.get(choice)
            if action is None:
                print("  unknown option")
                continue
            try:
                action(cfg)
            except KeyboardInterrupt:
                print("\n  (cancelled)")
    except (KeyboardInterrupt, EOFError):
        pass
    print("Bye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
