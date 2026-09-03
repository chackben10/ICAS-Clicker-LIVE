from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ScriptedHttpResponse:
    body: bytes = b"{}"
    status: int = 200
    content_type: str = "application/json"
    delay_seconds: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RecordedHttpRequest:
    method: str
    path: str
    query: str
    headers: dict[str, str]
    body: bytes
    # Source port of the peer socket. Requests sharing one port arrived on one
    # kept-alive connection, which is how connection reuse is asserted.
    client_port: int = 0


class ScriptedHttpServer(AbstractContextManager["ScriptedHttpServer"]):
    """A deterministic local HTTP peer for integration failure/recovery tests."""

    def __init__(self) -> None:
        self._responses: dict[str, deque[ScriptedHttpResponse]] = defaultdict(deque)
        self.requests: list[RecordedHttpRequest] = []
        self._lock = threading.Lock()
        controller = self

        class Handler(BaseHTTPRequestHandler):
            # ProPresenter's embedded server speaks HTTP/1.1 with keep-alive.
            # The default here is HTTP/1.0, which closes after every response and
            # would make a pooled client look identical to an unpooled one.
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                controller._handle(self)

            def do_POST(self) -> None:  # noqa: N802
                controller._handle(self)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="scripted-http")

    @property
    def port(self) -> int:
        return int(self._server.server_port)

    @property
    def host_port(self) -> str:
        return f"127.0.0.1:{self.port}"

    def enqueue(
        self,
        path: str,
        *,
        body: bytes | str | dict[str, Any] | list[Any] = b"{}",
        status: int = 200,
        content_type: str = "application/json",
        delay_seconds: float = 0.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        if isinstance(body, (dict, list)):
            encoded = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            encoded = body.encode("utf-8")
        else:
            encoded = body
        with self._lock:
            self._responses[path].append(
                ScriptedHttpResponse(encoded, status, content_type, delay_seconds, headers or {})
            )

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlsplit(handler.path)
        length = int(handler.headers.get("Content-Length", "0") or 0)
        body = handler.rfile.read(length) if length else b""
        with self._lock:
            self.requests.append(
                RecordedHttpRequest(
                    handler.command,
                    parsed.path,
                    parsed.query,
                    {key.casefold(): value for key, value in handler.headers.items()},
                    body,
                    int(handler.client_address[1]) if handler.client_address else 0,
                )
            )
            queue = self._responses.get(parsed.path)
            response = queue.popleft() if queue else ScriptedHttpResponse(b'{"error":"unscripted"}', 404)
        if response.delay_seconds:
            time.sleep(response.delay_seconds)
        try:
            handler.send_response(response.status)
            handler.send_header("Content-Type", response.content_type)
            handler.send_header("Content-Length", str(len(response.body)))
            for key, value in response.headers.items():
                handler.send_header(key, value)
            handler.end_headers()
            handler.wfile.write(response.body)
        except (BrokenPipeError, ConnectionResetError):
            # Expected when testing client-side timeouts.
            return

    def __enter__(self) -> "ScriptedHttpServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@dataclass(frozen=True)
class ScriptedObsResponse:
    data: dict[str, Any] = field(default_factory=dict)
    success: bool = True
    code: int = 100
    comment: str = ""
    delay_seconds: float = 0.0
    disconnect: bool = False


class ScriptedObsWebSocketServer(AbstractContextManager["ScriptedObsWebSocketServer"]):
    """Small OBS WebSocket v5 peer used by the real obsws-python client."""

    def __init__(self) -> None:
        from websockets.sync.server import serve

        self.requests: list[dict[str, Any]] = []
        self._responses: dict[str, deque[ScriptedObsResponse]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._server = serve(self._handle, "127.0.0.1", 0)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="scripted-obs-ws")

    @property
    def port(self) -> int:
        return int(self._server.socket.getsockname()[1])

    def enqueue(self, request_type: str, response: ScriptedObsResponse) -> None:
        with self._lock:
            self._responses[request_type].append(response)

    @staticmethod
    def _default_response(request_type: str) -> dict[str, Any]:
        if request_type == "GetVersion":
            return {
                "obsVersion": "31.0.0-test",
                "obsWebSocketVersion": "5.5.0-test",
                "rpcVersion": 1,
                "availableRequests": ["GetVersion", "GetCurrentProgramScene"],
                "supportedImageFormats": ["png"],
                "platform": "macos",
                "platformDescription": "Production Hub deterministic test peer",
            }
        if request_type == "GetCurrentProgramScene":
            return {"currentProgramSceneName": "Fixture Scene"}
        return {}

    def _handle(self, connection) -> None:
        try:
            connection.send(json.dumps({"op": 0, "d": {"obsWebSocketVersion": "5.5.0", "rpcVersion": 1}}))
            identify = json.loads(connection.recv())
            if identify.get("op") != 1:
                connection.close(code=4006, reason="Identify required")
                return
            connection.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
            while True:
                message = json.loads(connection.recv())
                if message.get("op") != 6:
                    continue
                request = message["d"]
                request_type = str(request["requestType"])
                with self._lock:
                    self.requests.append(request)
                    queue = self._responses.get(request_type)
                    scripted = queue.popleft() if queue else ScriptedObsResponse(
                        data=self._default_response(request_type)
                    )
                if scripted.delay_seconds:
                    time.sleep(scripted.delay_seconds)
                if scripted.disconnect:
                    connection.close()
                    return
                payload = {
                    "op": 7,
                    "d": {
                        "requestType": request_type,
                        "requestId": request["requestId"],
                        "requestStatus": {
                            "result": scripted.success,
                            "code": scripted.code,
                            "comment": scripted.comment,
                        },
                        "responseData": scripted.data or self._default_response(request_type),
                    },
                }
                connection.send(json.dumps(payload))
        except Exception:
            # Probe connections and deliberate reconnect tests close without an
            # OBS handshake. The client assertions retain the useful failure.
            return

    def __enter__(self) -> "ScriptedObsWebSocketServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)
