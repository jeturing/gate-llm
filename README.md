# gate-llm

**On-demand AI services on a single small GPU.**

[![tests](https://github.com/Jeturing/gate-llm/actions/workflows/tests.yml/badge.svg)](https://github.com/Jeturing/gate-llm/actions/workflows/tests.yml)

*Read this in Spanish: [README.es.md](README.es.md)*

A small GPU has room for one resident AI service — maybe two, if they are
small. But a business does not run one AI service: it runs a language model,
a voice, tomorrow a transcriber, and each of them sits in VRAM all day,
awake and idle, because nobody knows when the next request will come.

gate-llm removes the residency tax. A lightweight gatekeeper owns each
service's public port. The real backend sleeps **stopped**, holding no VRAM.
The first request wakes it: the gatekeeper starts the backend, waits until
its health endpoint reports the model loaded, and only then forwards the
request — **the cold request waits a few seconds instead of failing**.
After an idle window with nothing in flight, the gatekeeper stops the
backend again and the memory goes back to the pool, free for whatever
needs the GPU next.

One file of Python 3 standard library. No dependencies, no Kubernetes,
no cloud account.

## What this is — and what it is not

The idea of starting services on demand is old and honourable, and this
project does not claim to invent it:

* **systemd socket activation** has started services on first connection
  for years — at the level of a process and a socket, not of a GPU service
  with a model to load and a health contract.
* **Knative / KEDA** industrialised scale-to-zero on Kubernetes, with an
  activator that buffers requests during a cold start. gate-llm is what
  that pattern shrinks to when there is no cluster — one small GPU, a
  handful of services, one JSON file.
* **Ollama** loads a model on request and unloads it after an idle period
  (`keep_alive`, 5 minutes by default) — inside one engine, for its own
  models. gate-llm generalises the behaviour to *any* HTTP backend:
  containers, systemd units, plain processes.
* **llama-swap** (https://github.com/mostlygeek/llama-swap) is the closest
  neighbour: it starts local inference servers on demand when a request
  names their model, waits for their health endpoint, and unloads them
  after a TTL. It manages *models of inference engines* behind an endpoint
  routed by model name. gate-llm governs *whole system services* —
  heterogeneous, not only inference — behind a gatekeeper per service.
* **Sleepy LLM** (https://github.com/ac101m/sleepy-llm) wakes a single
  sleeping LLM backend — even the machine itself, over Wake-on-LAN —
  holding the request until it answers. No packaging, no measurement kit,
  one backend.
* **Sablier** (https://sablierapp.dev/) applies scale-to-zero to generic
  containers with a *blocking* strategy that holds the request — the
  infrastructure-level twin of the gatekeeper, with no inference layer:
  its "ready" is a started container, not a model loaded under a health
  contract.
* **Serverless GPU platforms** sell the same economics as a cloud service.
  gate-llm is for the GPU you already own.

What is specific here is the **complete, measured recipe** for making one
modest, owned GPU serve a company's several heterogeneous AI services —
an LLM today, a voice tomorrow — with request-holding
instead of request-failing, health-gated wake, safe idle stop, and real
numbers from a real machine, reproducible with the scripts in this repo.

## How it works

```
                        ┌─────────────────────────────┐
   caller ─── request ──▶  gate-llm (always on, tiny) │
                        │                             │
                        │  asleep? ── start backend ──▶  backend (LLM / TTS / STT)
                        │            wait for health     bound to loopback,
                        │            forward request     stopped when idle
                        │  idle window, 0 in flight ── stop backend
                        └─────────────────────────────┘
```

Four mechanisms, and only four:

1. **The gatekeeper holds the port.** Backends bind to loopback and are
   created with autostart disabled. Callers never change: same host, same
   port, same API.
2. **The cold request is held, not failed.** On the first request the
   gatekeeper runs the service's start command and *keeps the caller
   waiting* until the backend is healthy. Callers need a client timeout
   longer than the wake time — that is the pattern's central, deliberate
   trade-off.
3. **Health is the contract.** "Awake" means the health URL answers with
   an expected status. A process that started but is still loading its
   model receives no traffic.
4. **The stop is safe.** A background reaper stops the backend only after
   the idle window *and* with zero requests in flight. A long generation
   is never cut.

Built-in endpoints on every service port (they never count as activity):

| Endpoint | Effect |
|---|---|
| `GET /__gate-llm/status` | JSON state; never wakes the backend |
| `POST /__gate-llm/wake` | Wake now, wait until healthy |
| `POST /__gate-llm/sleep` | Stop now (409 while a request is in flight) |

## Measured on real hardware

Reference deployment: a virtual machine with ~8 GB RAM and a single
**Tesla P4 (8,192 MiB)**, serving a 27B-parameter LLM at 1-bit quantization
(llama.cpp, GGUF, ~3.8 GB on disk, ~4.5 GB of VRAM) and an open-source
zero-shot voice-cloning TTS engine (~3.4 GB of VRAM). Full methodology and
discussion in [the paper](docs/PAPER.md).

| Measurement | Value |
|---|---|
| Free VRAM with both services resident | 343 MiB |
| Free VRAM with the TTS asleep (LLM resident) | 3,628 MiB |
| Free VRAM with **everything asleep** | **8,192 MiB (0 in use)** |
| TTS cold wake (request → health with model loaded) | 24.5 s |
| LLM cold wake (engine ready; everyday operation, cache-dependent) | 4.5–15.2 s |
| LLM first full response, cold, end to end | ~14 s (short request) · ~24 s (streaming chat) |
| LLM warm — time to first token | 0.48–0.65 s |
| LLM warm — generation speed | 14.5 tok/s |
| LLM with a second stream in parallel | 8.2–8.3 tok/s |
| LLM first token with a cold prompt cache (engine already running) | 2.6–5.2 s |
| Reference idle windows | LLM 30 min · TTS 20 min |
| Host RAM, everything awake → everything asleep | ~10 GiB → ~2.0 GiB |
| LLM wake with the model file anchored in the OS page cache | **12.0–16.2 s** to engine ready, first token **13.6–18.1 s** (n=3, true cold: caches dropped). Same true-cold condition without the anchor: 23.4 s / 23.8 s. Cost: 3.54 GiB of RAM held permanently |

Independent replication, paired run: on Google Colab (Tesla T4, no
systemd), the full cycle passed on both sides of a controlled pair —
same model (Llama-3.2-3B-Instruct, Q4_K_M), same binary, same
gatekeeper, differing only in offload. **GPU**: wake to health 2.06 s,
bench median TTFT **0.366 s** at **110.23 tok/s**, 2,341 MiB of VRAM
awake and 0 asleep. **CPU** (offload off, 1 thread): 3.06 s to health,
TTFT **8.839 s** at **4.17 tok/s**. The pair also caught a silent
failure worth knowing: the official "CUDA" binary first ran on CPU
without a single warning, because its CUDA 12.8 libraries were not on
the loader path of a CUDA 13.0 container — full root cause, fix and
evidence in §6.6 of [the paper](docs/PAPER.md).

One honest caveat from the same deployment: the LLM's front door also
carries other, near-continuous traffic (on the order of a hundred requests
per hour, around the clock; the longest request-free gap in 3.3 days was
51 minutes). Even so, with the 30-minute window the model slept several
times in that period. **The idle window is the knob that decides how much
you save** — see the paper's limitations section before choosing yours.

## Supported environments

The kit **detects** its environment instead of assuming a Linux server —
an environment layer reports the OS, whether systemd is actually
operational, whether Docker exists, and where `nvidia-smi` lives (on
Windows it is not on the PATH). How each environment is served:

| Environment | Backend supervision | Gatekeeper residence |
|---|---|---|
| **Linux with systemd** | `systemd` units or `docker` containers | systemd unit |
| **Linux without systemd** — containers, WSL, **Google Colab** | `process`: detached spawn + PID record | detached process, or the host's own supervisor |
| **Windows** | `process` (or `docker` where Docker Desktop runs the backend) | Scheduled Task (see the honest limits below) |

Supervision is chosen **per service** in the config (`supervision`,
`docker_container`, `systemd_unit` — see the reference below), and the
pattern's rule sits above all of them: a backend is never stopped with
requests in flight, whatever starts and stops it. Honest limits: on
Windows the gatekeeper's residence is a **Scheduled Task, not a native
Windows service**, and the graceful stop of a plain process is
best-effort (`taskkill /T`, then `/F`); a restarted gatekeeper re-adopts
a `process` backend from its PID record only after its health endpoint
answers — it never kills a process it has not verified.

## Install (one command, recommended)

```bash
git clone https://github.com/Jeturing/gate-llm.git
cd gate-llm
python3 install.py        # Windows: py install.py
```

(`./install.sh` remains as a thin POSIX shortcut for the same command.)

The installer prints the detected environment, checks prerequisites and
**fails loudly, with the fix, when one is missing** (Python 3.8+, an
NVIDIA GPU via `nvidia-smi`, Docker only if your config drives
containers). It registers the gatekeeper's residence with whatever the
machine supports (systemd unit / Scheduled Task / detached process with
its generated launcher), creates the config from the example *only if
none exists* (your config is never overwritten — default locations:
`/etc/gate-llm/services.json` on Linux, `%ProgramData%\gate-llm\` on
Windows), starts the gatekeeper, and verifies the pattern end to end:
asleep → request → wakes → responds, with timings. Useful flags:
`--config FILE` (install your own config), `--skip-gpu-check` (CPU-only
services), `--strategy auto|systemd|task|process|none`, `--no-verify`.

Then edit the config to declare *your* services and restart the
gatekeeper (the menu's restart prompt knows the platform; on systemd:
`sudo systemctl restart gate-llm`).

Your backends must exist beforehand with autostart disabled — e.g. for
Docker: `docker create --restart no ...` — bound to loopback ports. The
gatekeeper starts and stops them; it does not create them. On Colab and
other notebook hosts there is nothing to pre-create: declare the service
with `supervision: "process"` and its plain start command.

### Manual install

```bash
sudo mkdir -p /opt/gate-llm /etc/gate-llm
sudo cp gate_llm_proxy.py gate_llm_env.py /opt/gate-llm/
sudo cp -r tools /opt/gate-llm/
sudo cp services.example.json /etc/gate-llm/services.json   # then edit it
sudo cp gate-llm.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now gate-llm
python3 /opt/gate-llm/gate_llm_proxy.py --config /etc/gate-llm/services.json --check
```

## Managing services: the menu

```bash
python3 /opt/gate-llm/tools/menu.py --config /etc/gate-llm/services.json
```

The menu edits the same JSON the gatekeeper reads — one source of truth.
It lists every service and model with its live state and VRAM, switches
the active model of a service (stopping and waking it cleanly), registers
new services and models, wakes/sleeps by hand, and tunes idle windows.
Every save is validated first (schema, duplicate ports, a bind probe on
new ports, a health check when the service is awake) and written
atomically with a `.bak`. Runtime edits (idle window, active model) are
picked up by the running gatekeeper within seconds; structural edits
(added/removed services, ports) need a restart, and the menu says so.

## Verifying and benchmarking your machine

```bash
# Full unattended cycle: asleep -> request -> awake -> responds -> idle -> asleep
GATE_URL=http://127.0.0.1:8080 REQUEST_PATH=/v1/models \
  python3 /opt/gate-llm/tools/verify_cycle.py
# (tools/verify_cycle.sh remains as a thin POSIX shortcut for the same check)

# The kit's own test suite (fake backends; no GPU needed) — the same
# command CI runs on Ubuntu and Windows runners on every push:
python3 tests/run_all.py

# Time to first token + tokens/second against any OpenAI-compatible endpoint
python3 /opt/gate-llm/tools/bench.py --base-url http://127.0.0.1:8080 --model your-model
```

Run them and you should find on your hardware what this README claims on
ours — that is the point of publishing the numbers with the recipe.

## Configuration reference

One JSON file (`services.example.json` is a complete, annotated example):

| Field | Meaning |
|---|---|
| `name` | Service id (used in logs, the menu, the status endpoint) |
| `listen_host` / `listen_port` | The public face the gatekeeper owns |
| `backend_host` / `backend_port` | The real backend, loopback only |
| `health_url` | Readiness contract; traffic flows only when it answers `health_expect_status` |
| `health_expect_status` | Expected HTTP statuses (default `[200]`; a loading llama.cpp answers 503) |
| `health_timeout_s` | Max wait for a wake before the held request fails with 502 |
| `idle_timeout_s` | Idle window before the stop; `0` = never sleep |
| `stop_grace_s` | Grace given to the backend on stop before forcing |
| `start` / `stop` | Command arrays, e.g. `docker start/stop` or `systemctl start/stop`. Omit `stop` and the gatekeeper owns the started process and stops its process group |
| `supervision` | Optional: `command` (default when a start/stop pair exists) or `process` — spawn `start` detached with a PID record; the portable strategy for Colab, systemd-less Linux and Windows |
| `docker_container` | Optional: container name; derives the `docker start/stop` pair (with the stop grace) |
| `systemd_unit` | Optional: unit name; derives the `systemctl start/stop` pair |
| `state_dir` | Optional: where PID records live (default: `.gate-llm-state` next to the config) |
| `models` + `active_model` | Optional: several start/stop pairs (e.g. two model sizes) and which one serves. The menu switches them |

## Limitations, stated plainly

* **Cold latency is real.** The first request after a sleep pays the wake
  (seconds to tens of seconds). Interactive callers with short timeouts
  will not tolerate it; batch and tolerant callers will. Choose the idle
  window per service accordingly.
* **One GPU is one GPU.** Two services awake at once must still fit in
  VRAM together; the pattern saves memory across *time*, not physics.
  Wake priority and queueing are future work — see
  [docs/MEJORAS.md](docs/MEJORAS.md).
* **Stateless backends only.** Stopping must be safe: a backend that keeps
  unsaved state in memory does not belong behind an idle stop.
* **A busy shared front door sleeps little.** If a service receives
  near-continuous traffic, savings depend entirely on the idle window
  being shorter than the natural gaps (see the measurement note above).
* The gatekeeper is a single small process: if it dies, its supervisor
  restarts it (systemd, the Windows task, or your service manager);
  backends already awake keep serving only through it, so treat it
  like any other piece of serving infrastructure.

## Documentation

* [Paper (EN, main)](docs/PAPER.md) · [Paper (ES)](docs/PAPER.es.md) — design,
  measurement methodology, full evidence, limitations, related work.
* [Improvements roadmap](docs/MEJORAS.md) — wake queues and priority,
  scheduled pre-warming, awake-minutes metering (the basis of usage
  billing), a light dialog model next to the large one, multi-GPU, a web
  menu, and where systemd socket activation fits better.

## License and authorship

MIT — see [LICENSE](LICENSE). Use it, replicate it, improve it, and if
you publish your numbers, say where the recipe came from.

Created and measured by **Jeturing** — https://github.com/Jeturing/gate-llm
