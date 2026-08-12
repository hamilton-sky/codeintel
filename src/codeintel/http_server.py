from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from codeintel.server import code_doctor_handler, code_query_handler, code_status_handler


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass  # suppress default stderr noise

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path not in ("/code/query", "/code/doctor"):
            self._send_json(404, {"error": "not-found"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self._send_json(400, {"error": "bad-request"})
            return
        raw = self.rfile.read(content_length) if content_length > 0 else b""
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"error": "bad-request"})
            return
        if not isinstance(parsed, dict):
            self._send_json(400, {"error": "bad-request"})
            return
        if self.path == "/code/doctor":
            result = code_doctor_handler(parsed)
        else:
            result = code_query_handler(parsed)
        self._send_json(200, result)

    def do_GET(self) -> None:
        if self.path != "/code/status":
            self._send_json(404, {"error": "not-found"})
            return
        result = code_status_handler({})
        self._send_json(200, result)


class CodeIntelHTTPServer(HTTPServer):
    pass


def run(host: str = "127.0.0.1", port: int = 8766) -> None:
    server = CodeIntelHTTPServer((host, port), _Handler)
    print(f"Listening on http://{host}:{port}")
    server.serve_forever()
