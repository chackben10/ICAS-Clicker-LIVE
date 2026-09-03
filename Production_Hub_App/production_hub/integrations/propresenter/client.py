from __future__ import annotations

import asyncio
import json
import threading
import urllib.parse
from typing import Any

import httpx

from production_hub.core.config.models import ProPresenterConfig
from production_hub.integrations.base import IntegrationBase


# Both derive from OSError, as urllib.error.URLError did, so callers that
# already handled transport failure keep working unchanged.
class ProPresenterTransportError(OSError):
    """ProPresenter could not be reached, or did not answer in time."""


class ProPresenterHttpError(OSError):
    """ProPresenter answered with a non-success status."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = int(code)


class ProPresenterClient(IntegrationBase):
    """Pooled HTTP client for ProPresenter's local API.

    Every call used to be `urllib.request.urlopen` inside `asyncio.to_thread`:
    a fresh TCP connection per request, plus thread-pool churn.  With a 4 Hz
    automation loop, a health check every four seconds, and two round-trips per
    clicker press, that was a constant drip of new sockets into ProPresenter's
    embedded server.  One keep-alive pool removes the connection setup from
    every call, including the ones on the operator's critical path.

    A client is created per event loop.  Production Hub runs the API on the main
    loop and clicker actions on the dispatcher's own loop, and an httpx pool
    belongs to the loop that created it.
    """

    def __init__(self, config: ProPresenterConfig) -> None:
        super().__init__("ProPresenter", config.enabled, f"{config.host}:{config.port}")
        self.config = config
        self._clients: dict[Any, httpx.AsyncClient] = {}
        self._clients_lock = threading.Lock()

    def url(self, path: str) -> str:
        path = path if path.startswith("/") else f"/{path}"
        return f"{self.config.base_url}{path}"

    @staticmethod
    def quote_segment(value: str) -> str:
        return urllib.parse.quote(str(value), safe="")

    def _client(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        with self._clients_lock:
            client = self._clients.get(loop)
            if client is not None and not client.is_closed:
                return client
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.config.request_timeout_seconds),
                limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
                trust_env=False,
            )
            self._clients[loop] = client
            return client

    async def aclose(self) -> None:
        """Close the pool belonging to the calling loop."""

        loop = asyncio.get_running_loop()
        with self._clients_lock:
            client = self._clients.pop(loop, None)
        if client is not None and not client.is_closed:
            await client.aclose()

    async def get_json(self, path: str) -> dict[str, Any]:
        body = await self.get_bytes(path, accept="application/json")
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.mark_error(f"Malformed ProPresenter JSON from {path}: {exc}")
            raise

    async def get_text(self, path: str) -> str:
        body = await self.get_bytes(path, accept="text/plain")
        return body.decode("utf-8", errors="replace")

    async def get_bytes(self, path: str, accept: str = "application/json") -> bytes:
        try:
            response = await self._client().get(self.url(path), headers={"accept": accept})
        except httpx.HTTPError as exc:
            # httpx raises timeouts with an empty message; an empty last_error
            # would leave the health check reporting Connected after a failure.
            message = str(exc) or type(exc).__name__
            self.mark_error(message)
            raise ProPresenterTransportError(message) from exc
        if response.status_code >= 400:
            message = f"HTTP Error {response.status_code}: {response.reason_phrase}"
            self.mark_error(message)
            raise ProPresenterHttpError(response.status_code, message)
        self.mark_success()
        return response.content

    async def trigger(self, path: str) -> bool:
        await self.get_bytes(path, accept="application/json")
        return True

    async def trigger_macro(self, macro_name: str) -> bool:
        encoded = self.quote_segment(macro_name)
        return await self.trigger(f"/macro/{encoded}/trigger")

    async def trigger_presentation(self, uuid: str) -> bool:
        encoded = self.quote_segment(uuid)
        return await self.trigger(f"/presentation/{encoded}/trigger")

    async def trigger_audio(self, playlist: str, track: str) -> bool:
        playlist_q = self.quote_segment(playlist)
        track_q = self.quote_segment(track)
        return await self.trigger(f"/audio/playlist/{playlist_q}/{track_q}/trigger")
