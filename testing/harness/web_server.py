from __future__ import annotations

import json
import mimetypes
import threading
import time
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from urllib.parse import unquote, urlsplit

from testing.harness.paths import REPOSITORY_ROOT


PRESENTATION = {
    "presentation": {
        "id": {"uuid": "fixture-presentation", "name": "Fixture Presentation"},
        "destination": "presentation",
        "groups": [
            {
                "name": "Fixture Group",
                "slides": [
                    {
                        "text": "Fixture slide",
                        "label": "C Major Pads",
                        "enabled": True,
                        "notes": "",
                    }
                ],
            }
        ],
    }
}


def api_payload(path: str) -> tuple[bytes, str]:
    if path == "/health":
        return b"OK\n", "text/plain; charset=utf-8"
    if path in {"/active-presentation", "/presentation/fixture-presentation"}:
        return json.dumps(PRESENTATION).encode(), "application/json"
    if path == "/slide-index":
        payload = {"presentation_index": {"index": 0}, "presentation": {"id": {"uuid": "fixture-presentation"}}}
        return json.dumps(payload).encode(), "application/json"
    if path == "/playlist/focused":
        payload = {"id": {"uuid": "fixture-playlist", "name": "Fixture Playlist"}, "items": []}
        return json.dumps(payload).encode(), "application/json"
    if path.startswith("/playlist/"):
        return json.dumps({"id": {"uuid": "fixture-playlist"}, "items": []}).encode(), "application/json"
    if path == "/score":
        payload = {"revision": 0, "rows": [{"id": "fixture", "name": "Fixture", "score": 1}], "history": []}
        return json.dumps(payload).encode(), "application/json"
    if path == "/service_logos":
        return json.dumps([{"name": "Fixture Logo", "uuid": "fixture-logo"}]).encode(), "application/json"
    if path == "/macros":
        return json.dumps(["Bible Macro", "Presentation Macro"]).encode(), "application/json"
    if path == "/audio/playlists":
        return json.dumps(["Major Pads", "Minor Pads", "Neutral Pads"]).encode(), "application/json"
    if path == "/audio/tracks":
        return json.dumps(["C Major Pads", "D Major Pads"]).encode(), "application/json"
    if path == "/audio/active":
        return b"C Major Pads", "text/plain; charset=utf-8"
    if path in {"/auto-show", "/clicker-presentation-activation"}:
        return json.dumps({"enabled": True}).encode(), "application/json"
    if path == "/presentation-focus-time":
        return json.dumps({"enabled": True, "seconds": 10}).encode(), "application/json"
    if path == "/song-library/search":
        return json.dumps({"items": []}).encode(), "application/json"
    if path in {"/scene/current", "/current-base"}:
        return json.dumps({"name": "ProPresenter Input", "base": "/presentation/active"}).encode(), "application/json"
    if path.startswith("/api/") or path.startswith("/admin/") or path == "/debug":
        return json.dumps({"ok": True, "items": [], "integrations": []}).encode(), "application/json"
    return json.dumps({"ok": True, "items": []}).encode(), "application/json"


class SafePageHandler(BaseHTTPRequestHandler):
    server_version = "ProductionHubTestServer/1"
    request_counts: dict[tuple[str, str], int] = {}
    request_counts_lock = threading.Lock()

    def _test_context(self) -> tuple[str, str]:
        cookies = SimpleCookie()
        cookies.load(self.headers.get("Cookie", ""))
        scenario = cookies.get("ph_test_scenario")
        run_id = cookies.get("ph_test_run")
        return (
            scenario.value if scenario else "populated",
            run_id.value if run_id else "default",
        )

    def log_message(self, _format: str, *_args) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length:
            self.rfile.read(length)
        path = urlsplit(self.path).path
        scenario, _run_id = self._test_context()
        if scenario == "conflict" and path == "/score":
            self._send(409, json.dumps({"error": "revision_conflict", "actual": 2}).encode(), "application/json")
            return
        body, content_type = api_payload(path)
        self._send(200, body, content_type)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        asset = path.removeprefix("/remote/").lstrip("/")
        if not asset:
            asset = "index.html"
        candidate = (REPOSITORY_ROOT / asset).resolve()
        root = REPOSITORY_ROOT.resolve()
        if candidate.is_file() and root in candidate.parents:
            content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            self._send(200, candidate.read_bytes(), content_type)
            return
        scenario, run_id = self._test_context()
        if scenario == "loading":
            time.sleep(0.25)
        if scenario == "offline":
            self._send(503, json.dumps({"ok": False, "error": "offline"}).encode(), "application/json")
            return
        if scenario == "reconnect":
            key = (f"{scenario}:{run_id}", path)
            with self.request_counts_lock:
                count = self.request_counts.get(key, 0)
                self.request_counts[key] = count + 1
            if count == 0:
                self._send(503, json.dumps({"ok": False, "error": "reconnecting"}).encode(), "application/json")
                return
        if scenario == "empty":
            if path == "/score":
                body = json.dumps({"revision": 0, "rows": [], "history": []}).encode()
            elif path == "/audio/active":
                self._send(200, b"", "text/plain; charset=utf-8")
                return
            else:
                body = json.dumps({"items": []}).encode()
            self._send(200, body, "application/json")
            return
        body, content_type = api_payload(path)
        self._send(200, body, content_type)


@contextmanager
def safe_page_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), SafePageHandler)
    thread = threading.Thread(target=server.serve_forever, name="production-hub-test-web", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
