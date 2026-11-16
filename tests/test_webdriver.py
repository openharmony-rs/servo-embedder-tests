import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from servo_embedder_tests.webdriver import connect


class _Server(BaseHTTPRequestHandler):
    """A WebDriver server that creates sessions and nothing else."""

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps({"value": {"sessionId": "s", "capabilities": {"browserName": "servo"}}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_connecting_ignores_proxy_settings(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Server)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    try:
        assert connect(server.server_address[1], timeout=10, grace=0).session_id == "s"
    finally:
        server.shutdown()
