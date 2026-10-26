#!/usr/bin/env python3
"""
bench.py -- measure any OpenAI-compatible chat endpoint the way a voice or
chat front-end feels it: time to first token (TTFT) and generation speed
(tokens/second), over streamed responses.

Pure Python 3 standard library. Works against llama.cpp server, vLLM,
Ollama's OpenAI-compatible endpoint, or a gate-llm port (in which case the
first run also measures the cold wake: expect one slow run, then warm runs).

Examples:
    python3 bench.py --base-url http://127.0.0.1:8080 --model local-27b
    python3 bench.py --base-url http://127.0.0.1:8080 --runs 5 --json

Notes:
* Generation tok/s is computed as completion_tokens / (total - TTFT).
* The token count comes from the server's own `usage` block when the
  server reports it (stream_options.include_usage). Otherwise the number
  of content chunks is counted and the result is flagged "estimated",
  because one chunk is not always exactly one token.
"""

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request


def bench_once(base_url, path, model, prompt, max_tokens, api_key, timeout):
    url = base_url.rstrip("/") + path
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    start = time.time()
    ttft = None
    chunks = 0
    usage_tokens = None
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            if obj.get("usage"):
                usage_tokens = obj["usage"].get("completion_tokens")
            choices = obj.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                if delta.get("content"):
                    if ttft is None:
                        ttft = time.time() - start
                    chunks += 1
    total = time.time() - start
    if ttft is None:
        raise RuntimeError("stream ended without any content token")
    tokens = usage_tokens if usage_tokens else chunks
    estimated = usage_tokens is None
    gen_time = max(total - ttft, 0.001)
    return {
        "ttft_s": round(ttft, 3),
        "total_s": round(total, 3),
        "tokens": tokens,
        "tokens_estimated": estimated,
        "gen_tok_s": round(tokens / gen_time, 2),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="bench.py",
        description="TTFT + tokens/second bench for OpenAI-compatible endpoints.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--path", default="/v1/chat/completions")
    parser.add_argument("--model", default="local")
    parser.add_argument("--api-key", default=None)
    parser.add_argument(
        "--prompt",
        default="Explain, in four or five sentences, why a small GPU benefits "
                "from services that sleep when idle and wake on demand.")
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--json", action="store_true", help="print JSON only")
    args = parser.parse_args(argv)

    results = []
    for i in range(args.runs):
        try:
            result = bench_once(args.base_url, args.path, args.model,
                                args.prompt, args.max_tokens, args.api_key,
                                args.timeout)
        except (urllib.error.URLError, RuntimeError) as exc:
            print(f"run {i + 1}: FAILED: {exc}", file=sys.stderr)
            return 1
        results.append(result)
        if not args.json:
            flag = " (token count estimated from chunks)" if result["tokens_estimated"] else ""
            print(f"run {i + 1}: TTFT {result['ttft_s']:.3f}s | "
                  f"{result['gen_tok_s']:.2f} tok/s | {result['tokens']} tokens | "
                  f"total {result['total_s']:.3f}s{flag}")
    summary = {
        "runs": len(results),
        "ttft_median_s": round(statistics.median(r["ttft_s"] for r in results), 3),
        "gen_tok_s_median": round(statistics.median(r["gen_tok_s"] for r in results), 2),
        "results": results,
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"median: TTFT {summary['ttft_median_s']:.3f}s | "
              f"{summary['gen_tok_s_median']:.2f} tok/s "
              f"(first run is the cold one when the service was asleep)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
