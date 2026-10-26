# gate-llm: on-demand AI services on a single small GPU

**A measured pattern for making one modest, owned GPU serve several heterogeneous AI services, sleeping and waking on demand**

Jeturing — October 2026 · Text and code licensed under MIT
Repository: https://github.com/Jeturing/gate-llm

*Versión en español (secundaria): [PAPER.es.md](PAPER.es.md)*

---

## Abstract

Companies that self-host AI face uncomfortable arithmetic:
every service — a language model, a synthesised voice, a transcriber —
is deployed as a permanent resident holding video memory (VRAM) around
the clock, although its real use is intermittent and bursty. On a small
GPU — the hardware a company actually owns — two residents
already exhaust the card, and the third service simply does not exist.

This work presents **gate-llm**, a minimal and complete pattern for
removing that residency cost: a lightweight *gatekeeper* (a single
Python file, standard library only, no dependencies) owns each
service's public port; the real backend stays stopped, holding no
VRAM; the first request wakes it — and **waits**, held by the
gatekeeper, until the backend's health endpoint declares the model
loaded —; and after an idle window with no request in flight, the
gatekeeper stops it safely.

The pattern is validated with measurements from a real deployment: a
Tesla P4 with 8 GB serving a 27B-parameter LLM quantised to 1 bit and
a voice synthesis engine. With both resident, free VRAM was 343 MiB;
with everything asleep, the whole card — 8,192 MiB — is free. The cost
is cold latency (24.5 s for the measured wake of the voice service;
4.5–15.2 s for the LLM), paid only by the first request after each
sleep. The paper describes the design, the measurement methodology,
the full evidence, the limitations — including the lesson from a
deployment with near-continuous traffic, where the idle window turned
out to be the variable that decides the savings — and the honest
relationship with prior art, of which this pattern is a deliberate
reduction, not an invention.

## 1. The problem

Self-hosting AI has become cheap in compute and expensive in
*residency*. Modern inference engines load the model into VRAM at
start and keep it there: correct for latency, ruinous for a small GPU
shared by several sporadically used services:

- A 27B LLM quantised to 1 bit holds ~4.5 GB of resident VRAM.
- A voice-cloning synthesis engine holds ~3.4 GB resident.
- A Tesla P4 has 8 GB.

Two services already sum to ~7.9 GB: the card is full *while idle*.
Adding a third service (transcription, a second, lighter model,
vision) is impossible without removing another. The usual alternatives
— one GPU per service, or renting serverless GPU in the cloud —
contradict the very reason to self-host: known cost, data at home,
hardware already paid for.

The observation that organises the design: **the real use of these
services comes in bursts**. An internal assistant answers during work
hours; the voice synthesises in batches; the large model analyses when
someone asks. Permanent residency pays 24 hours of VRAM for minutes
of work.

## 2. Design

### 2.1 Components

- **The gatekeeper** (`gate_llm_proxy.py`): one process per machine,
  always on, negligible footprint (standard-library Python, threads).
  It listens on each registered service's public port and forwards
  HTTP traffic — including streaming, which token-by-token responses
  require — to the real backend.
- **The backends**: the AI services as they already exist (a llama.cpp
  server in a container, a TTS engine, tomorrow an STT), created with
  autostart disabled and bound to *loopback* ports. Nothing in them
  knows about the gatekeeper.
- **The configuration**: a single JSON file declares each service:
  ports, health URL, idle window and the start/stop commands
  (`docker start/stop`, `systemctl start/stop`, or a direct process
  owned by the gatekeeper). It is the only source of truth; the
  management menu (section 5) edits that same file.

### 2.2 The four mechanisms

1. **Port ownership.** The caller (an application, another service, a
   browser) calls the same host and port as always. Replacing the
   backend with the gatekeeper is invisible: same API, same port.
2. **Holding the cold request.** If the backend is asleep, the
   gatekeeper runs its start command and *holds* the caller's request
   — with a per-service lock, so N simultaneous cold requests share a
   single wake — until the backend declares health or the configured
   deadline expires (then, and only then, the request receives an
   explicit 502). No request is lost for arriving "too early".
3. **Health as the readiness contract.** The gatekeeper polls the
   health URL until it returns an expected status (200 in the measured
   engines; a llama.cpp still loading its model answers 503, and that
   detail is exactly what separates "process started" from "service
   ready"). Traffic flows only with health verified.
4. **Safe idle stop.** A per-service reaper stops the backend when
   *both* conditions hold: the idle window has elapsed since the last
   request **and** zero requests are in flight. A long generation in
   progress is never interrupted by the timer.

Auxiliary mechanisms: built-in status/wake/sleep endpoints on the
port itself (`/__gate-llm/status|wake|sleep`, which never count as
activity), and hot reload of runtime parameters (window, active
model) when the configuration changes on disk — with one safety rule:
the start/stop pair and the active model apply only while the service
is asleep, so a stop can never point at the wrong process.

### 2.3 Choosing a service's model

A service may declare several *models* (start/stop pairs with their
own identity — e.g. a large 27B LLM and a light 4B one behind the same
port) and one active model. Switching is: stop (if awake), flip the
active model in the configuration, and wake. The gatekeeper always
serves the active one; the rest sleep at no cost.

### 2.4 Warm page-cache anchor (optional)

The LLM's wake time depends on the state of the operating system's
page cache. In everyday operation — the condition behind the
4.5–15.2 s range reported in §4 — the model file is sometimes still
resident in RAM and sometimes must come from disk. An optional
refinement takes the disk out of the equation: *anchor* the model
file in the page cache — read it into cache at boot and pin it there
(locked pages, in the manner of tools such as `vmtouch`), so the
file's pages survive cache pressure and gatekeeper restarts, and
every wake reads the model from RAM. The backends still stop and
release all VRAM; what stays resident is only the file's pages in
ordinary system RAM.

Measured on the reference deployment, under a controlled condition
distinct from the everyday range above: with the caches deliberately
dropped (a true cold start) and no anchor, the engine was ready in
**23.4 s** and the first token arrived at **23.8 s** (consumer
route, streaming chat). With the anchor in place — the 3.8 GB model
file verified 100 % resident and locked — three wakes by the same
route gave engine-ready in **12.0–16.2 s** and first token at
**13.6–18.1 s**: the true-cold worst case is roughly halved. The
cost is **3.54 GiB of RAM held permanently** on a 16 GiB machine:
the asleep floor rises from ~2.0 GiB to ~5.7 GiB, while the awake
peak does not grow — the anchored pages are shared with the
server's own mapping of the file — leaving ~10 GiB of headroom.
The variance that remains no longer comes from the model file: it
comes from container start-up, binary and library pages, and CUDA
initialisation.

### 2.5 What it does not try to be

It is not an orchestrator: it does not create backends, replicate
them, or balance across machines. It is not a model server: it does
not touch inference. It is, deliberately, the thin layer missing
between "the service exists" and "the service holds the GPU only
while it works".

### 2.6 Portability: the environment is detected, not assumed

The gatekeeper and every tool in the kit are Python standard
library, and an environment layer (`gate_llm_env.py`) detects at
start-up what the machine actually offers — operating system, an
*operational* systemd (not merely an installed one), Docker, the
`nvidia-smi` binary (on Windows it lives in NVIDIA's own
directories, not on a POSIX-style PATH), hosted-notebook
environments such as Colab — instead of assuming a Linux server.
Supervision of each backend is a strategy, chosen per service:

| Environment | Backend supervision | Gatekeeper residence |
|---|---|---|
| Linux with systemd | `systemd` units or `docker` containers | systemd unit |
| Linux without systemd — containers, WSL, Colab | `process`: detached spawn with a PID record | detached process, or the host's own supervisor |
| Windows | `process` (or `docker` where Docker Desktop runs the backend) | Scheduled Task — stated plainly: a task, not a native Windows service |

The `process` strategy is the portable floor: spawn the backend
detached (`start_new_session` on POSIX;
`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on Windows), record
its PID in a small JSON file, and stop gracefully first — process
group SIGTERM on POSIX; on Windows `taskkill /T`, escalating to
`/F`, where the graceful step is best-effort: an honest limit of
that platform without a service wrapper. A restarted gatekeeper
re-adopts a backend from its PID record only after the health
contract answers for it; it never kills a process it has not
verified is its backend. The safe-stop rule — never stop with
requests in flight — sits above every strategy: supervision decides
*how* a backend starts and stops, never *whether* it is safe to
stop it.

The installer (`install.py`) is the same code on both operating
systems: it prints the detected environment, chooses the residence
the machine supports, and finishes by verifying the cycle. And the
kit's test suite — fake backends, no GPU required — runs in
continuous integration on Ubuntu and Windows runners, so the
portability claim is re-checked publicly on every push, not merely
asserted here.

## 3. Measurement methodology

Reference deployment: a virtual machine with ~8 GB of RAM and a
single **Tesla P4 (8,192 MiB)** passed through to the VM; a Linux
system with systemd and Docker. Services:

- **LLM**: a 27B-parameter model, 1-bit quantisation (GGUF Q1_0
  format, ~3.8 GB on disk), served by llama.cpp behind the
  gatekeeper. Measured resident footprint: ~4.5 GB of VRAM.
- **TTS**: an open-source zero-shot voice-cloning engine, served in a
  container behind the gatekeeper. Measured resident footprint:
  ~3.4 GB of VRAM.

Procedures:

- **VRAM**: `nvidia-smi` (used/free memory per process and in total)
  in each state: both resident, TTS asleep, everything asleep.
- **Cold wake**: a real request through the gatekeeper with the
  backend stopped; measured until (a) health with the model loaded
  and (b) the first complete end-to-end response, by request type.
- **Warm performance**: an in-house bench (`tools/bench.py`,
  included) against the OpenAI-compatible endpoint: streamed
  completions, a fixed working prompt (~460 tokens), sustained
  generations of ~120 tokens, several runs per condition; reporting
  time to first token (TTFT) and generation tokens/second. The
  "contention" condition generates a second stream in parallel,
  reproducing real use of the shared front door.
- **Full cycle**: `tools/verify_cycle.sh` (included) runs asleep →
  request → awake → responds → idle → asleep, with VRAM at each
  stage, unattended.
- **Real traffic**: an audit of the deployment's own logs over 3.3
  days to characterise demand (section 6.4).

Every figure in this paper comes from these measurements; where a
value depends on cache or prior state, the observed range is reported
and the dependency is stated.

## 4. Results

### 4.1 Memory: the card becomes a shared pool again

| State | Free VRAM (of 8,192 MiB) |
|---|---|
| Both services resident (before the pattern) | 343 MiB |
| TTS asleep, LLM resident | 3,628 MiB |
| **Everything asleep** | **8,192 MiB (0 MiB in use)** |

Host RAM followed the same movement: from ~10 GiB with everything
resident to ~2.0 GiB with everything asleep. In other words: outside
the real minutes of work, the machine recovers both of its scarce
resources in full.

### 4.2 The cost of waking

| Service | Cold wake |
|---|---|
| TTS | **24.5 s** until health with the model loaded; the first synthesis after waking was valid |
| LLM | **4.5–15.2 s** until the engine was ready, depending on cache state; first complete end-to-end response: **~14 s** (short decision request) and **~24 s** (streaming chat) |
| LLM, with the model file anchored in the page cache (§2.4) | **12.0–16.2 s** to engine ready, first token **13.6–18.1 s** (n=3 wakes, true cold: caches dropped). Under the same true-cold condition without the anchor: 23.4 s / 23.8 s. Cost: 3.54 GiB of RAM held |
| LLM, third-party replication, paired run — GPU (Colab, T4, 3B, offload on; §6.6) | Cycle **PASS**; **2.06 s** to health; bench median TTFT **0.366 s**, **110.23 tok/s**; VRAM awake 2,341 MiB, asleep 0 MiB |
| LLM, third-party replication, paired run — CPU (Colab, 3B, offload off, 1 thread; §6.6) | Cycle **PASS**; **3.06 s** to health; bench median TTFT **8.839 s**, **4.17 tok/s** — the same pair's GPU side is ≈26× faster in generation |

Waking is the pattern's single, visible price, paid once per sleep:
by the first request. Every subsequent request, until the idle
window lapses again, is warm.

### 4.3 Warm performance (unchanged from resident)

| Condition | TTFT | Generation |
|---|---|---|
| No contention | 0.48–0.65 s | 14.5 tok/s |
| A second stream in parallel | stretched | 8.2–8.3 tok/s |
| Cold prompt cache, engine already running | 2.6–5.2 s | 14.5 tok/s |

The pattern adds no measurable warm latency (the gatekeeper is a
local streaming forward); the contention observed belongs to the
engine sharing the GPU between two streams, not to the gatekeeper.

### 4.4 Reference windows

The windows the reference deployment runs in production — and which
the repository's example ships — are **30 min for the LLM** and
**20 min for the TTS**: long enough to absorb the natural pauses of
a working session without paying wakes, short enough for the card to
spend the night free.

## 5. Adoption: one command and one menu

An infrastructure pattern only exists if installing it costs less
than the problem it solves. The package includes:

- **A one-command installer** (`install.py` — the same Python file
  on Linux and Windows; `install.sh` remains as a thin POSIX
  shortcut): it detects the environment (§2.6), checks prerequisites
  and fails loudly, with the fix, when one is missing (Python; a GPU
  visible to `nvidia-smi`; Docker only if the config uses it);
  registers the gatekeeper's residence with whatever the machine
  supports — a systemd unit, a Windows Scheduled Task, or a detached
  process with its generated launcher; creates the configuration
  from the template *never overwriting* an existing one; and
  finishes by verifying the pattern against the first service
  (asleep → request → wakes → responds, with timings). It is
  idempotent: running it twice is safe.
- **A terminal menu** (`tools/menu.py`, standard library): it lists
  services and models with live state and VRAM; switches the active
  model applying the stop–switch–wake cycle; registers and removes
  services and models; wakes and sleeps by hand; tunes windows. It
  validates before saving (schema, ports in use — including a real
  bind probe —, health reachable while the service is awake) and
  writes atomically with a backup.
- **A replication kit**: the TTFT/tok·s bench and the full-cycle
  verification script, so whoever replicates the pattern can obtain
  — and publish — their own numbers with the same procedure.

## 6. Limitations and lessons

### 6.1 Cold latency excludes certain callers

An interactive caller with a 5 s timeout will not survive a 24.5 s
wake. The pattern serves tolerant callers (batches, internal tools,
assistants where waiting is acceptable, APIs with a configured
timeout) and harms impatient ones. Partial mitigation — scheduled or
event-driven pre-warming — is on the roadmap, not in this version.

### 6.2 VRAM is shared across time, not multiplied

Two services awake at once must still fit together. If real demand
makes everything coincide, the pattern degrades to "all resident"
and the saving disappears: the gain exists because real use is
bursty and staggered. When two wakes compete for the same VRAM, this
version serialises them in arrival order; a priority queue is
future work.

### 6.3 Stateless backends only (or state safely stored)

Stopping on idle requires the stop to be harmless: sessions or
half-finished jobs inside the backend are lost. The usual inference
and synthesis engines comply (their state is the model, which is
reloaded); a backend with unsaved in-memory state does not.

### 6.4 The lesson of continuous traffic: the window decides

The 3.3-day audit of the reference deployment produced the project's
most instructive result: the LLM's front door also carries other,
near-continuous traffic — on the order of a hundred requests per
hour, around the clock — and the longest request-free gap in those
3.3 days was **51 minutes**. With an idle window longer than the
natural gaps, the model would barely have slept and the pattern
would have been decorative. With the window at **30 minutes**, the
model slept **several times** in that same period, and the card was
freed for whole hours overnight.

The generalisable lesson: **on a shared, busy service, the saving is
not decided by the pattern but by the window**. Measuring the
traffic's natural gaps first (an audit of the logs suffices) and
setting the window below the typical gap is part of the recipe, not
a tuning detail.

### 6.5 A single gatekeeper

The gatekeeper is one process per machine: it is also a single
point of passage. Its supervisor restarts it if it dies (a systemd
unit, a scheduled task, a service manager — §2.6), and asleep
backends lose nothing; but an awake backend only serves through it.
At the scale of one GPU and one company, that is the
same level of risk as the inference engine itself; at another
scale, it is not.

### 6.6 Third-party replication (Colab): the cycle holds, and a warning

The claim of §9 — whoever replicates the recipe should find on
their hardware what is claimed here of ours — was exercised on
foreign hardware: a hosted notebook environment (Google Colab)
with a **Tesla T4 (15,360 MiB)**, no systemd and no Docker. The
gatekeeper ran as a plain detached process — the `process`
strategy of §2.6, under `nohup`; the environment had taken port
8080 for itself, so the gatekeeper listened on 8082 — and the
backend was the official llama.cpp release binary (build b11889,
"CUDA 12.8") serving Llama-3.2-3B-Instruct (Q4_K_M, 1.9 GB)
through the gatekeeper, with a 75 s idle window.

The first session produced the replication's real finding, and it
is methodological. The cycle passed in full — `verify_cycle`
reported **PASS**: asleep → held request → awake → responds →
idle window → asleep, waking to health in **4.6 s** with the
first complete response at **9.7 s**. But with the backend awake
and serving, **the VRAM never moved**: 0 MiB in use, free VRAM
identical asleep and awake (14,913 MiB), the process living in
system RAM (~3.6 GB) with not a single CUDA line in its log. The
official "CUDA" binary was running the model on CPU, in silence,
despite `-ngl 99`.

A follow-up session established the root cause and closed the
replication properly. The official b11889 binary was built
against **CUDA 12.8**: its `libggml-cuda.so` requires
`libcudart.so.12` and `libcublas.so.12`, which the container —
equipped with a CUDA 13.0 toolchain — does not put on the loader
path. With those libraries unresolvable, the CUDA backend never
loads, `--list-devices` prints *(none)*, and `-ngl 99` silently
degrades to CPU, with no error even at full verbosity. The fix
was a loader path, not a rebuild: `LD_LIBRARY_PATH` pointing at
the pip wheels already installed in the image
(`nvidia/cuda_runtime/lib` and `nvidia/cublas/lib`) **plus
`/usr/lib64-nvidia`** — without that last directory, the driver
library `libcuda.so.1` itself stays unresolved. (A from-source
build with `cmake -DGGML_CUDA=ON` also completed successfully,
but the pair below was served by the fixed official binary, so
both halves share one binary.) With the fix in place,
`--list-devices` lists `CUDA0: Tesla T4`, the startup log shows
58 layer assignments to CUDA0 and 0 to CPU, and VRAM awake is
**2,338 MiB** for the backend process — the movement whose
absence gave the first session away.

The paired run — same model, same binary, same gatekeeper, same
test set (`tools/bench.py`, five runs; `tools/verify_cycle.py`),
differing only in offload:

| Colab pair (Tesla T4 host) | GPU — offload on (`-ngl 99`) | CPU — offload off (`-ngl 0`, 1 thread¹) |
|---|---|---|
| Wake to health | **2.06 s** | **3.06 s** |
| First complete response | **2.27 s** | **4.95 s** |
| VRAM awake | **2,341 MiB** total (2,338 MiB the process) | **219 MiB** (CUDA context only; no layers offloaded) |
| Bench median TTFT | **0.366 s** | **8.839 s** |
| Bench median generation | **110.23 tok/s** | **4.17 tok/s** |
| Cycle (`verify_cycle`) | **PASS** — cold request HTTP 200 in 2.01 s | **PASS** — cold request HTTP 200 in 3.05 s |

¹ The binary's own default thread count; no `-t` flag was passed.

Both halves idle-slept back to 0 MiB in use. The GPU side of the
pair is roughly **26× faster in generation and 24× in TTFT** than
its CPU side — which is what the first session's warm figures
(TTFT ≈ 8.8 s, ≈ 4.4 tok/s) actually were: single-threaded CPU
numbers, close to the deliberate CPU run above. Those first-run
figures are historical, labelled CPU, and superseded as the
pair's CPU side by the deliberate run.

The warning generalises to anyone replicating, on any stack: **a
backend can say "CUDA" and run on CPU in silence; the signal that
gives it away is VRAM that does not move.** Compare VRAM awake
against asleep — the gatekeeper's own status reports it — before
believing any performance number, including ours.

## 7. Related work

This pattern is not born in a vacuum; it is a conscious reduction of
consolidated ideas to the minimal case of an owned GPU:

- **Socket activation (systemd)**: the operating system starts the
  service on the first connection and hands it the accepted socket.
  It is the direct ancestor; it operates on processes and sockets,
  with no notion of health, of a model still loading, or of VRAM,
  and without the orderly holding of concurrent cold requests that
  the gatekeeper resolves here.
- **Knative Serving**: its *Activator* receives traffic when there
  are no replicas, holds requests, triggers scale-from-zero, and
  forwards them once the backend is ready — the same shape as this
  gatekeeper, carried by a whole Kubernetes control plane. **KEDA**
  adds event-driven scale-to-zero in the same ecosystem. gate-llm
  takes the shape and removes the cluster.
- **Ollama (`keep_alive`)**: one concrete engine that loads the
  model on the first request and unloads it after 5 idle minutes by
  default. It demonstrates the appetite for the behaviour; gate-llm
  decouples it from the engine and extends it to any HTTP backend,
  with a full process stop (not just a model unload) and several
  services coordinated over the same GPU.
- **llama-swap** (https://github.com/mostlygeek/llama-swap): the
  closest neighbour. A proxy that starts local inference servers on
  demand when a request names their model, waits until their health
  endpoint answers before serving, and unloads them after an idle
  TTL. Its unit of management is the *model of an inference engine*,
  routed by model name behind a single endpoint. gate-llm governs
  *whole system services* — systemd units and containers,
  heterogeneous and not only inference — behind a gatekeeper per
  service, packaged as a measured recipe for an owned 8 GB GPU
  serving a company's heterogeneous AI services.
- **Sleepy LLM** (https://github.com/ac101m/sleepy-llm): a minimal
  proxy that wakes a sleeping LLM backend — even the machine
  itself, over Wake-on-LAN — holding the request until the backend
  answers. It manages a single backend, with no packaging,
  measurement kit or multi-service governance: independent,
  near-simultaneous evidence that the core gesture is in the air.
- **Sablier** (https://sablierapp.dev/): generic scale-to-zero for
  containers, with a *blocking* strategy that holds the request
  until the workload is up. It is the infrastructure-level twin of
  the gatekeeper, without an inference layer: its "ready" is a
  started container, not a model loaded under a health contract.
- **Engine-internal load and unload**: llama.cpp's router mode
  loads and unloads models inside a server that itself stays alive,
  and vLLM's sleep mode offloads weights without stopping the
  process. Both act *inside* one engine; the gatekeeper stops and
  starts *whole services*, whichever engine — or no inference
  engine at all — they run.
- **Commercial serverless GPU** (RunPod, Modal, Baseten, Replicate
  and others): they sell wake-on-demand as a service, with cold
  starts measured in seconds and usage billing. They are the market
  proof of the pattern and, at the same time, its essential
  opposite: there the GPU is someone else's; here it is yours.

The contribution claimed is narrow and checkable — not the
wake-on-demand proxy itself, which exists, as the neighbours above
show, but the complete recipe around it: one gatekeeper per service
governing heterogeneous whole services (an LLM, a voice) taking
turns on a single owned 8 GB GPU, an installer and a management
menu, measured operating rules (health traffic neither wakes the
backend nor counts as activity; never stop with requests in
flight), and published wake, latency and contention numbers —
working and measured on the hardware a company actually has.

## 8. Open improvements

[MEJORAS.md](MEJORAS.md) develops the known routes, each with its
gain, its cost and its risk: a wake queue with priority when two
services compete for VRAM; scheduled pre-warming; measuring
"awake minutes" per service and per client as the basis of usage
billing; a light dialog model resident next to the large one on
demand; multi-GPU; systemd socket activation where it fits better
than the gatekeeper; and a web interface for the menu. None of them
is implemented in this version, and the text does not present them
as existing.

## 9. Conclusions

A small GPU is not small: its *residency* is. With a gatekeeper that
holds the cold request instead of failing it, a health contract that
distinguishes "started" from "ready", and a stop that never cuts
work in flight, a company's AI services can share a single
8 GB card, sleeping between uses — measured: from 343 MiB free with
everything resident to 8,192 MiB free with everything asleep —
paying one known cost, the wake, only on the first request of each
burst. The variable the operator must govern is not technical but
observational: the idle window against the real gaps in their
traffic.

Whoever replicates the recipe with the included kit should find on
their hardware what is claimed here of ours. That is the paper's
central claim, and it is written so it can be refuted with numbers.

## References

1. systemd.socket — socket-based service activation.
   https://www.freedesktop.org/software/systemd/man/systemd.socket.html
2. Knative Serving — autoscaling and scale-to-zero; the Activator.
   https://knative.dev/docs/
3. KEDA — event-driven scaling for Kubernetes. https://keda.sh
4. Ollama — documentation and API (`keep_alive`, idle unload).
   https://github.com/ollama/ollama
5. llama.cpp — the inference server used in the measurements.
   https://github.com/ggerganov/llama.cpp
6. NVIDIA System Management Interface (`nvidia-smi`) — VRAM
   measurement. https://developer.nvidia.com/
7. llama-swap — proxy for local inference servers with on-demand
   start and idle unload. https://github.com/mostlygeek/llama-swap
8. Sleepy LLM — wake-on-demand proxy for a sleeping LLM backend.
   https://github.com/ac101m/sleepy-llm
9. Sablier — scale-to-zero for containers with a blocking
   strategy. https://sablierapp.dev/

---

*gate-llm is free software (MIT). Text and measurements: Jeturing,
October 2026. The figures correspond to the reference deployment
described in section 3; your hardware will give others — the
replication kit exists to measure them.*
