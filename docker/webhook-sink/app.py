"""Small local webhook capture service for Docker development."""

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EVENTS = []
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, payload, content_type="application/json"):
        body = payload if isinstance(payload, bytes) else payload.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/events"):
            with LOCK:
                events = list(EVENTS)
            if self.path == "/events":
                self._send(200, json.dumps(events, indent=2))
                return
            rows = "".join(
                f"<article><time>{event['received_at']}</time><pre>{json.dumps(event, indent=2)}</pre></article>"
                for event in reversed(events)
            ) or "<p>No webhook events received.</p>"
            self._send(200, f"<!doctype html><title>FlowOps Webhook Sink</title><h1>FlowOps Webhook Sink</h1><p>{len(events)} event(s)</p>{rows}", "text/html; charset=utf-8")
            return
        if self.path == "/health":
            self._send(200, '{"status":"ok"}')
            return
        self._send(404, '{"detail":"not found"}')

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = raw.decode("utf-8", "replace")
        event = {
            "received_at": datetime.now(timezone.utc).isoformat(),
            "path": self.path,
            "headers": {key: value for key, value in self.headers.items()},
            "body": body,
        }
        with LOCK:
            EVENTS.append(event)
        self._send(202, json.dumps({"status": "accepted", "event_count": len(EVENTS)}))

    def log_message(self, *_args):
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
