"""A local HTTP server for fixture pages, reachable from the device through a reverse port forward."""

from __future__ import annotations

import http.server
import json
import mimetypes
import re
import socket
import threading
import time
import urllib.parse
from collections.abc import Callable, Sequence
from importlib import resources
from pathlib import Path
from typing import cast

# Pages POST JSON here (see pages/report.js); tests read it with `PageServer.wait_for_report`.
REPORT_PATH = "/__report"


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def scenario_server(self) -> _Server:
        return cast(_Server, self.server)

    def setup(self) -> None:
        super().setup()
        # Servo opens many parallel requests to the same host; Nagle plus delayed ACKs would stall them.
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        self.scenario_server.requests.append(path)
        if path in self.scenario_server.dynamic:
            body, content_type = self.scenario_server.dynamic[path]
            self._send(200, body, content_type)
            return
        file = self.scenario_server.find(path)
        if file is None:
            self._send(404, b"not found", "text/plain")
            return
        content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        body = file.read_bytes()
        if content_type == "text/html":
            body = self.scenario_server.inject(path, body)
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        self._send(200, body, content_type)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if path != REPORT_PATH:
            self._send(404, b"not found", "text/plain")
            return
        try:
            report = json.loads(body)
        except ValueError:
            self._send(400, b"bad json", "text/plain")
            return
        with self.scenario_server.condition:
            self.scenario_server.reports.append(report)
            self.scenario_server.condition.notify_all()
        self._send(204, b"", "text/plain")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, roots: list[Path]) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.roots = roots
        self.dynamic: dict[str, tuple[bytes, str]] = {}
        self.requests: list[str] = []
        self.reports: list[dict] = []
        self.condition = threading.Condition()
        self.injections: list[tuple[str, bytes]] = []

    def inject(self, path: str, body: bytes) -> bytes:
        """`body` with the snippets for `path` inserted at the start of its `<head>`."""
        path = urllib.parse.unquote(path)
        snippets = b"".join(snippet for prefix, snippet in self.injections if path.startswith(prefix))
        if not snippets:
            return body
        for tag in (rb"<head\b[^>]*>", rb"<html\b[^>]*>", rb"<!doctype[^>]*>"):
            if m := re.search(tag, body, re.IGNORECASE):
                return body[: m.end()] + snippets + body[m.end() :]
        return snippets + body

    def find(self, path: str) -> Path | None:
        relative = urllib.parse.unquote(path).lstrip("/") or "index.html"
        for root in self.roots:
            candidate = (root / relative).resolve()
            if candidate.is_relative_to(root.resolve()) and candidate.is_file():
                return candidate
        return None


class PageServer:
    """Serves the bundled `pages/` directory, extra directories and pages added at runtime."""

    def __init__(self, extra_roots: Sequence[Path] = ()) -> None:
        bundled = Path(str(resources.files("servo_embedder_tests") / "pages"))
        self._server = _Server([*extra_roots, bundled])
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> PageServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}/{path.lstrip('/')}"

    def add(self, path: str, html: str, content_type: str = "text/html; charset=utf-8") -> str:
        """Serves `html` at `path` and returns its URL."""
        path = "/" + path.lstrip("/")
        self._server.dynamic[path] = (html.encode(), content_type)
        return self.url(path)

    def inject(self, prefix: str, html: str) -> None:
        """Inserts `html` at the start of the `<head>` of the HTML files served under `prefix`."""
        self._server.injections.append((prefix, html.encode()))

    @property
    def requests(self) -> list[str]:
        return self._server.requests

    @property
    def reports(self) -> list[dict]:
        """JSON objects the pages reported, in arrival order."""
        return self._server.reports

    def clear_reports(self) -> None:
        with self._server.condition:
            self._server.reports.clear()

    def wait_for_report(self, predicate: Callable[[dict], bool], timeout: float = 10, since: int = 0) -> dict | None:
        """First report at index >= `since` that matches `predicate`, waiting up to `timeout` seconds for it."""
        deadline = time.monotonic() + timeout
        with self._server.condition:
            while True:
                for report in self._server.reports[since:]:
                    if predicate(report):
                        return report
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._server.condition.wait(remaining)
