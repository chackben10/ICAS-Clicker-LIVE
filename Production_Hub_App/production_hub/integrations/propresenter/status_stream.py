from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, BinaryIO


StatusUpdateCallback = Callable[[str, Any], None]


class ProPresenterStatusStream:
    """Own one official aggregated status subscription instead of repeated polls."""

    # Every subscribed path must map onto the same endpoint the direct
    # ProPresenter getter uses, or the cache answers a different question than
    # the caller asked.  "presentation/current" reports the presentation the
    # operator has selected in the library, which is NOT /presentation/active
    # (what is live on screen), so it is deliberately not subscribed here.
    PATHS = (
        "look/current",
        "presentation/slide_index",
        "status/slide",
    )

    def __init__(
        self,
        base_url: str,
        callback: StatusUpdateCallback,
        *,
        logger: Any | None = None,
        timeout_seconds: float = 12.0,
        reconnect_seconds: float = 2.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.callback = callback
        self.logger = logger
        self.timeout_seconds = max(5.0, float(timeout_seconds))
        self.reconnect_seconds = max(0.1, float(reconnect_seconds))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._response: BinaryIO | None = None
        self.connected = False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="production-hub-propresenter-status",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
        self.connected = False

    @staticmethod
    def parse_update(payload: bytes | str) -> tuple[str, Any] | None:
        try:
            decoded = payload.decode("utf-8") if isinstance(payload, bytes) else payload
            update = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(update, dict):
            return None
        path = str(update.get("url") or "").strip().lstrip("/")
        if path.startswith("v1/"):
            path = path[3:]
        return (path, update.get("data")) if path else None

    def _run(self) -> None:
        body = json.dumps(list(self.PATHS)).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/status/updates?chunked=true",
            data=body,
            method="POST",
            headers={"accept": "application/json", "content-type": "application/json"},
        )
        while not self._stop_event.is_set():
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    self._response = response
                    self.connected = True
                    if self.logger is not None:
                        self.logger.info(
                            "propresenter_status_stream_connected",
                            "ProPresenter event stream connected",
                        )
                    self._consume(response)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if not self._stop_event.is_set() and self.logger is not None:
                    self.logger.warning(
                        "propresenter_status_stream_reconnecting",
                        "ProPresenter event stream disconnected; retrying",
                        error=str(exc),
                    )
            finally:
                self.connected = False
                self._response = None
            self._stop_event.wait(self.reconnect_seconds)

    def _consume(self, response: BinaryIO) -> None:
        buffer = bytearray()
        while not self._stop_event.is_set():
            line = response.readline()
            if not line:
                return
            if line.strip():
                buffer.extend(line)
                continue
            if not buffer:
                continue
            parsed = self.parse_update(bytes(buffer))
            buffer.clear()
            if parsed is not None:
                self.callback(*parsed)
