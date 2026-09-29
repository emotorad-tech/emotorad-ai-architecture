"""Test doubles for anything that speaks HTTP.

`FakeServer` is a real server on 127.0.0.1 with an ephemeral port, so the
transport is exercised through urllib exactly as it runs in production.
`FakeTransport` stands in for the transport itself when a test only cares about
the body a client builds and how it reads the answer.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeServer:
    def __init__(self):
        self.requests = []
        self._responses = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                outer.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": json.loads(raw or b"{}")}
                )
                if outer._responses:
                    status, body, delay = outer._responses.pop(0)
                else:
                    status, body, delay = 500, {"error": "no response queued"}, 0.0
                if delay:
                    time.sleep(delay)
                payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    def queue(self, status, body, delay=0.0):
        self._responses.append((status, body, delay))

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()


class FakeTransport:
    """Replays queued bodies (or raises queued exceptions) and records every post."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def post(self, path, body, timeout=None):
        self.calls.append({"path": path, "body": body, "timeout": timeout})
        if not self.responses:
            raise AssertionError("FakeTransport ran out of queued responses")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
