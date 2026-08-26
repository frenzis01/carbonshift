#!/usr/bin/env python3
"""Minimal stand-in executor for docker-compose end-to-end testing.

Not part of the CarbonShift codebase's engine/service — just a stdlib-only
stub that plays the role of the external "IP:PORT" executor described in the
architecture: it accepts a dispatch, "executes" it (a short sleep), then
POSTs the result back to the callback_url it was given.
"""
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            payload = {}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"accepted": true}')

        threading.Thread(target=self._execute_and_callback, args=(payload,), daemon=True).start()

    def _execute_and_callback(self, payload):
        request_id = payload.get("request_id")
        callback_url = payload.get("callback_url")
        print(f"[mock-executor] executing request_id={request_id} flavour={payload.get('flavour')}", flush=True)
        time.sleep(1)  # pretend to do work

        if not callback_url:
            print("[mock-executor] no callback_url in payload, nothing to report", flush=True)
            return

        result = {
            "success": True,
            "result": {"echo": payload.get("payload"), "executed_by": "mock-executor"},
        }
        req = urllib.request.Request(
            callback_url,
            data=json.dumps(result).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                print(f"[mock-executor] callback delivered, status={resp.status}", flush=True)
        except Exception as e:
            print(f"[mock-executor] callback failed: {e}", flush=True)

    def log_message(self, fmt, *args):
        print("[mock-executor] " + (fmt % args), flush=True)


def main():
    server = ThreadingHTTPServer(("0.0.0.0", 9000), Handler)
    print("[mock-executor] listening on :9000", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
