# Measurements — 2026-11-07 — server-01 (Apple M2 Pro, 16 GB)

Second deployment measured with the paper's own method (`tools/bench.py`,
section 3): fixed working prompt (~460 tokens), streamed generations of
~127 tokens, **through the gateway endpoint** — never against the backend
directly. Client ran on the same host; LAN clients would add their network
latency on top of these figures.

## Deployment

| Piece | Value |
|---|---|
| Host | MacBook Pro, Apple M2 Pro (GPU 16 cores), 16 GB unified memory, macOS 27.2 |
| Gatekeeper | The house LLM gateway (Hermes gateway, FastAPI + Redis queue), `:8080`, concurrency 1 |
| Backend | LM Studio (`llmster`), MLX runtime, `:1234` |
| Model | `deepseek-coder-v2-lite-instruct` — DeepSeek-Coder-V2-Lite-Instruct 4-bit (16B total, 2.4B active, MoE); **8.85 GB** loaded, context 8,192 |

## Warm performance (5 runs)

| Metric | Value |
|---|---|
| TTFT, median | **0.141 s** (warm runs 0.137–0.145 s) |
| Generation, median | **78.17 tok/s** (warm runs 77.97–78.29 tok/s) |
| First run of the session | TTFT 8.05 s — engine idle spin-up / cold prompt cache; stated per section 3's rule that state dependencies are reported, not hidden |

## Contention (two streams in parallel, 3 runs each)

| Stream | TTFT median | Generation median |
|---|---|---|
| 1 | 0.572 s | 42.11 tok/s |
| 2 | 0.579 s | 42.48 tok/s |

As in section 4.3, the split belongs to the engine sharing itself between
streams, not to the gatekeeper.

## Memory: asleep vs awake (unified memory)

| State | Host memory used | Free |
|---|---|---|
| Model loaded (awake) | ~14 GiB | 1,150 MiB |
| Model unloaded (asleep) | 5,895 MiB | **9,870 MiB** |

Sleeping returns the model's ~8.85 GB to the shared pool in full — the
section 4.1 effect, on unified memory instead of VRAM.

## Cold wake through the gate

With the model unloaded, a single bench request through the gateway woke
the backend via its just-in-time load:

| Metric | Value |
|---|---|
| TTFT (= the wake) | **11.646 s** |
| First complete response | 13.155 s |
| Generation after wake | 84.19 tok/s |

The backend also re-sleeps on its own: the loaded model carries a 60-minute
idle TTL (`lms ps`), so the cycle repeats without any external timer.

## The gateway's own accounting

The gateway measures what passes through it: its Jeturing-AI surface
(`GET /v1/usage`, per-tenant API key) recorded the verification traffic of
this session for tenant `server-01` — 1 classify call, 158 tokens in /
13 tokens out — and its chat surface reports `usage` per completion. The
front door is also the meter.

## Caveats

- One client, on localhost, one model, one evening — a deployment record,
  not a benchmark claim about the hardware class.
- The 8.05 s first-run outlier depends on backend idle state; it is the
  same species of state dependency section 6.6 warns about, and is kept
  visible for that reason.
