#!/usr/bin/env python3
"""
fake_backend.py -- a test double for the gate-llm kit: an HTTP backend
that "loads its model" for a while (health answers 503) and then serves

  GET  /health                503 while loading, then 200
  GET  /v1/models             a one-model list
  POST /v1/chat/completions   an SSE stream of N chunks at a fixed
                              interval (~1/interval tokens per second),
                              with a final usage block

Used by the tests in this directory and by the GitHub Actions workflow.
No GPU, no model, no dependencies -- Python 3 standard library only.

    python3 tests/fake_backend.py --port 18081 --load-seconds 2
"""

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    started_at = time.time()
    load_s = 0.0
    chunk_interval = 0.05     # ~20 tok/s
    chunks = 24

    def log_message(self, *args):
        pass

    def _send(self, code, body, content_type="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            if time.time() - Handler.started_at < Handler.load_s:
                self._send(503, json.dumps({"status": "loading"}))
            else:
                self._send(200, json.dumps({"status": "ok"}))
        elif self.path == "/v1/models":
            self._send(200, json.dumps({"data": [{"id": "fake-1"}]}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.path != "/v1/chat/completions":
            self._send(404, json.dumps({"error": "not found"}))
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(payload):
            data = ("data: " + payload + "\r\n\r\n").encode("utf-8")
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

        for _ in range(Handler.chunks):
            chunk(json.dumps({"choices": [{"delta": {"content": "tok "}}]}))
            time.sleep(Handler.chunk_interval)
        chunk(json.dumps({"choices": [],
                          "usage": {"completion_tokens": Handler.chunks}}))
        end = b"data: [DONE]\r\n\r\n"
        self.wfile.write(f"{len(end):x}\r\n".encode() + end + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def main():
    parser = argparse.ArgumentParser(prog="fake_backend.py")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--load-seconds", type=float, default=0.0)
    parser.add_argument("--chunk-interval", type=float, default=0.05)
    parser.add_argument("--chunks", type=int, default=24)
    args = parser.parse_args()
    Handler.load_s = args.load_seconds
    Handler.chunk_interval = args.chunk_interval
    Handler.chunks = args.chunks
    Handler.started_at = time.time()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"fake backend on {args.host}:{args.port} "
          f"(load {args.load_seconds}s)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
