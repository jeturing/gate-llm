#!/usr/bin/env python3
"""
gate_llm_env -- the environment layer of gate-llm.

Everything in gate-llm that used to *assume* its surroundings lives here
instead, detected at runtime:

* which operating system this is (Windows or POSIX),
* whether systemd is actually operational (not merely installed),
* whether the Docker CLI is present,
* where `nvidia-smi` lives -- on Windows it is usually NOT on PATH (it
  sits in the NVIDIA driver directories), so PATH is only the first
  place looked,
* whether this looks like a Google Colab runtime (no systemd, no
  Docker, one borrowed GPU).

On top of detection, this module carries the portable process toolbox
the gatekeeper and the tools share:

* `spawn_detached` -- start a long-lived child that outlives us, the
  same way on both platforms (new session on POSIX; DETACHED_PROCESS |
  CREATE_NEW_PROCESS_GROUP on Windows),
* `terminate_pid` -- stop a process (and its tree/group) with a grace
  period and a forced finish, without POSIX-only calls leaking into
  callers,
* PID records -- small JSON files that let a restarted gatekeeper
  re-adopt a backend it spawned before dying,
* `split_command` / `join_command` -- command-line parsing with the
  platform's real rules (shlex on POSIX, CommandLineToArgvW on
  Windows, through ctypes).

Supervision strategies for a backend, resolved by the gatekeeper from
the service configuration:

* `command` -- explicit start/stop command arrays (the original
  gate-llm behaviour; `docker start/stop`, `systemctl start/stop`, a
  script of yours),
* `systemd` -- the config names a `systemd_unit`; the commands are
  derived,
* `docker` -- the config names a `docker_container`; the commands are
  derived,
* `process` -- the gatekeeper spawns the `start` array itself as a
  detached process, tracks it with a PID record and stops it by PID.
  This is the strategy that works everywhere: Colab, a Linux box
  without systemd, and Windows.

Pure Python 3 standard library. No dependencies.
"""

import ctypes
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

IS_WINDOWS = os.name == "nt"
IS_POSIX = os.name == "posix"
OS_NAME = "windows" if IS_WINDOWS else ("posix" if IS_POSIX else os.name)

# Windows creation flags (winbase.h). DETACHED_PROCESS: the child gets
# no console and is not killed when ours closes. CREATE_NEW_PROCESS_GROUP:
# it leads its own group, so it can be signalled as a unit.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------

def which(name):
    """Absolute path of an executable, or None (PATHEXT-aware)."""
    return shutil.which(name)


def which_or(name):
    """Path if found, else the bare name (so errors name the tool)."""
    return shutil.which(name) or name


def find_nvidia_smi():
    """Locate nvidia-smi without assuming it is on PATH.

    Windows installs rarely put it on PATH: it lives under the NVIDIA
    driver directories. POSIX fallbacks cover the usual prefixes.
    """
    found = shutil.which("nvidia-smi")
    if found:
        return found
    candidates = []
    if IS_WINDOWS:
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        candidates = [
            os.path.join(program_files, "NVIDIA Corporation", "NVSMI",
                         "nvidia-smi.exe"),
            os.path.join(system_root, "System32", "nvidia-smi.exe"),
            os.path.join(program_files, "NVIDIA Corporation", "NVSMI",
                         "nvidia-smi"),
        ]
    else:
        candidates = ["/usr/bin/nvidia-smi", "/usr/local/bin/nvidia-smi",
                      "/opt/nvidia/bin/nvidia-smi"]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def systemd_operational():
    """True only if systemd is PID 1's system and answers -- not merely
    installed. (A container or a WSL/Colab shell can ship the binary
    with nothing behind it.)"""
    if IS_WINDOWS or not shutil.which("systemctl"):
        return False
    if not os.path.isdir("/run/systemd/system"):
        return False
    try:
        out = subprocess.run(["systemctl", "is-system-running"],
                             capture_output=True, text=True, timeout=5)
        state = (out.stdout or "").strip()
        return state in ("running", "degraded", "starting",
                         "initializing", "maintenance")
    except Exception:
        return False


def docker_available():
    return shutil.which("docker") is not None


def docker_daemon_ok(timeout=6):
    if not docker_available():
        return False
    try:
        out = subprocess.run([shutil.which("docker"), "info"],
                             capture_output=True, text=True, timeout=timeout)
        return out.returncode == 0
    except Exception:
        return False


def is_colab():
    if os.environ.get("COLAB_GPU") is not None or \
            os.environ.get("COLAB_RELEASE_TAG") is not None:
        return True
    try:
        import importlib.util
        return os.path.isdir("/content") and \
            importlib.util.find_spec("google.colab") is not None
    except Exception:
        return False


def scheduled_task_exists(name):
    """Windows only: is there a scheduled task with this name?"""
    if not IS_WINDOWS:
        return False
    try:
        out = subprocess.run([which_or("schtasks"), "/Query", "/TN", name],
                             capture_output=True, text=True, timeout=10)
        return out.returncode == 0
    except Exception:
        return False


def detect_environment():
    """A plain dict describing where we are running. Stable keys."""
    smi = find_nvidia_smi()
    return {
        "os": OS_NAME,
        "is_windows": IS_WINDOWS,
        "is_posix": IS_POSIX,
        "python": "%d.%d.%d" % sys.version_info[:3],
        "python_executable": sys.executable,
        "systemd": systemd_operational(),
        "systemctl": which("systemctl"),
        "docker": docker_available(),
        "docker_daemon": docker_daemon_ok() if docker_available() else False,
        "nvidia_smi": smi,
        "gpu_present": smi is not None,
        "colab": is_colab(),
    }


def default_config_candidates():
    """Where an installed gate-llm keeps services.json, per platform."""
    if IS_WINDOWS:
        base = os.environ.get("ProgramData", r"C:\ProgramData")
        return [os.path.join(base, "gate-llm", "services.json")]
    return ["/etc/gate-llm/services.json"]


def default_config_path():
    for candidate in default_config_candidates():
        if os.path.exists(candidate):
            return candidate
    return "services.json"


# ---------------------------------------------------------------------------
# GPU queries (through the found nvidia-smi, anywhere)
# ---------------------------------------------------------------------------

def _run_smi(args, timeout=8):
    smi = find_nvidia_smi()
    if not smi:
        return ""
    try:
        out = subprocess.run([smi] + args, capture_output=True, text=True,
                             timeout=timeout)
        return (out.stdout or "").strip()
    except Exception:
        return ""


def gpu_totals():
    """(free_mib, total_mib) of the first GPU, or None."""
    out = _run_smi(["--query-gpu=memory.free,memory.total",
                    "--format=csv,noheader,nounits"])
    if not out:
        return None
    try:
        free, total = out.splitlines()[0].split(",")
        return int(free.strip()), int(total.strip())
    except (ValueError, IndexError):
        return None


def gpu_name():
    out = _run_smi(["--query-gpu=name", "--format=csv,noheader"])
    return out.splitlines()[0].strip() if out else None


def gpu_compute_apps():
    """[(pid, used_mib), ...] for processes holding GPU memory."""
    out = _run_smi(["--query-compute-apps=pid,used_memory",
                    "--format=csv,noheader,nounits"])
    apps = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2:
            try:
                apps.append((int(parts[0]), int(parts[1])))
            except ValueError:
                pass
    return apps


# ---------------------------------------------------------------------------
# portable process control
# ---------------------------------------------------------------------------

def spawn_detached(argv, cwd=None, log_path=None):
    """Spawn a long-lived child detached from this process.

    POSIX: new session (its own process group, immune to our terminal).
    Windows: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP.
    Returns the subprocess.Popen handle.
    """
    if log_path:
        log_fh = open(log_path, "ab")  # noqa: SIM115 - handed to the child
        stdout = stderr = log_fh
    else:
        stdout = stderr = subprocess.DEVNULL
    kwargs = {
        "cwd": cwd,
        "stdin": subprocess.DEVNULL,
        "stdout": stdout,
        "stderr": stderr,
        "close_fds": True,
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
    elif IS_POSIX:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs)


def pid_alive(pid):
    """True if a process with this pid exists (best effort, portable)."""
    if not pid or pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if handle:
                kernel32.CloseHandle(handle)
                return True
            # ERROR_ACCESS_DENIED (5) means it exists but is not ours.
            return ctypes.get_last_error() == 5
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def terminate_pid(pid, grace_s=30):
    """Stop a process (group/tree where the platform allows), gracefully
    first, forcibly after the grace period. Returns True when the pid is
    gone (or was already). Never raises.

    POSIX: the gatekeeper spawns backends as process-group leaders, so
    SIGTERM goes to the whole group, then SIGKILL.
    Windows: `taskkill /T` asks the tree to close, then `/T /F` forces
    it. The graceful phase is best-effort there -- a detached console
    process may only honour the forced phase; that asymmetry is
    documented, not hidden.
    """
    pid = int(pid)
    if not pid_alive(pid):
        return True
    try:
        if IS_WINDOWS:
            taskkill = which_or("taskkill")
            subprocess.run([taskkill, "/PID", str(pid), "/T"],
                           capture_output=True, timeout=15)
        elif IS_POSIX:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                os.kill(pid, signal.SIGTERM)
    except Exception:
        pass
    deadline = time.time() + max(float(grace_s), 0.0)
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    # Forced finish.
    try:
        if IS_WINDOWS:
            subprocess.run([taskkill, "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        elif IS_POSIX:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                os.kill(pid, signal.SIGKILL)
    except Exception:
        pass
    deadline = time.time() + 10
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    return not pid_alive(pid)


# ---------------------------------------------------------------------------
# PID records (a restarted gatekeeper can re-adopt its backends)
# ---------------------------------------------------------------------------

def state_dir_for(config_path, override=None):
    """Directory holding PID records and gatekeeper state."""
    if override:
        return override
    env_dir = os.environ.get("GATE_LLM_STATE_DIR")
    if env_dir:
        return env_dir
    base = os.path.dirname(os.path.abspath(config_path)) if config_path else "."
    return os.path.join(base, ".gate-llm-state")


def pid_record_path(state_dir, service_name):
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in service_name)
    return os.path.join(state_dir, safe + ".pid.json")


def write_pid_record(path, pid, argv):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"pid": int(pid), "argv": list(argv),
                       "started": time.time()}, fh)
        os.replace(tmp, path)
    except OSError:
        pass  # state is a convenience; never let it break a wake


def read_pid_record(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            record = json.load(fh)
        if isinstance(record, dict) and record.get("pid"):
            return record
    except (OSError, ValueError):
        pass
    return None


def clear_pid_record(path):
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# command lines, with each platform's real parsing rules
# ---------------------------------------------------------------------------

def split_command(line):
    """Split a command line the way the platform's programs will see it."""
    if IS_WINDOWS:
        try:
            return _split_windows(line)
        except Exception:
            return shlex.split(line)
    return shlex.split(line)


def _split_windows(line):
    """CommandLineToArgvW via ctypes -- the exact rules Windows applies."""
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    shell32.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p,
                                            ctypes.POINTER(ctypes.c_int)]
    count = ctypes.c_int(0)
    argv = shell32.CommandLineToArgvW(line, ctypes.byref(count))
    if not argv:
        raise OSError("CommandLineToArgvW failed")
    try:
        return [argv[i] for i in range(count.value)]
    finally:
        kernel32.LocalFree(argv)


def join_command(argv):
    if IS_WINDOWS:
        return subprocess.list2cmdline([str(a) for a in argv])
    return shlex.join([str(a) for a in argv])


def summary_line(env=None):
    """One human line describing the detected environment (for logs)."""
    env = env or detect_environment()
    bits = [env["os"], "python " + env["python"]]
    bits.append("systemd" if env["systemd"] else "no systemd")
    bits.append("docker" if env["docker"] else "no docker")
    bits.append("GPU via " + env["nvidia_smi"] if env["nvidia_smi"]
                else "no nvidia-smi found")
    if env["colab"]:
        bits.append("Colab runtime")
    return " · ".join(bits)
