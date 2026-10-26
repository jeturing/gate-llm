# gate-llm: on-demand AI services on a single small GPU

**A measured pattern for making one modest, owned GPU serve several heterogeneous AI services, sleeping and waking on demand**

Jeturing — October 2026 · Text and code licensed under MIT
Repository: https://github.com/Jeturing/gate-llm

*Versión en español (secundaria): [PAPER.es.md](PAPER.es.md)*

---

## Abstract

Small organisations that self-host AI face uncomfortable arithmetic:
every service — a language model, a synthesised voice, a transcriber —
is deployed as a permanent resident holding video memory (VRAM) around
the clock, although its real use is intermittent and bursty. On a small
GPU — the hardware a small business actually owns — two residents
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

The LLM's wake time was observed to depend on the state of the
operating system's page cache: 4.5 s when the model file is still
cached in RAM, up to 15.2 s when it must come from disk. An optional
refinement removes that variance: *anchor* the model file in the
page cache (read it into cache at boot and keep it there, in the
manner of tools such as `vmtouch`), so every wake is a warm wake and
the disk leaves the equation. The backends still stop and release
all VRAM; what stays resident is only the file's pages in ordinary
system RAM, which the kernel can still reclaim under memory
pressure. The effect of the anchor on the wake distribution is
being measured on the reference deployment and will be incorporated
as an update to this document; until then, the honest figure is
the baseline of the previous paragraph — a wake that varies with
cache state between 4.5 s and 15.2 s — which is precisely the
variance the anchor is meant to remove.

### 2.5 What it does not try to be

It is not an orchestrator: it does not create backends, replicate
them, or balance across machines. It is not a model server: it does
not touch inference. It is, deliberately, the thin layer missing
between "the service exists" and "the service holds the GPU only
while it works".

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
| LLM, with the model file anchored in the page cache (§2.4) | Measurement in progress; to be incorporated as a document update. Baseline without the anchor: 4.5–15.2 s depending on cache state |

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

- **A one-command installer** (`install.sh`): it checks prerequisites
  and fails loudly, with the fix, when one is missing (Python,
  systemd, a GPU visible to `nvidia-smi`, Docker if the config uses
  it); installs the gatekeeper as a systemd service; creates the
  configuration from the template *never overwriting* an existing
  one; and finishes by verifying the pattern against the first
  service (asleep → request → wakes → responds, with timings). It is
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
point of passage. systemd restarts it if it dies, and asleep
backends lose nothing; but an awake backend only serves through it.
At the scale of one GPU and one small organisation, that is the
same level of risk as the inference engine itself; at another
scale, it is not.

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
  service, packaged as a measured recipe for one small owned GPU.
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
working and measured on the hardware a small organisation
actually has.

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
work in flight, an organisation's AI services can share a single
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
