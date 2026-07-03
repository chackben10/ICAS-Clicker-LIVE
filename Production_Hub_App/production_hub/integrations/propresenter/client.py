from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from production_hub.core.config.models import ProPresenterConfig
from production_hub.integrations.base import IntegrationBase


class ProPresenterClient(IntegrationBase):
    def __init__(self, config: ProPresenterConfig) -> None:
        super().__init__("ProPresenter", config.enabled, f"{config.host}:{config.port}")
        self.config = config

    def url(self, path: str) -> str:
        path = path if path.startswith("/") else f"/{path}"
        return f"{self.config.base_url}{path}"

    @staticmethod
    def quote_segment(value: str) -> str:
        return urllib.parse.quote(str(value), safe="")

    async def get_json(self, path: str) -> dict[str, Any]:
        body = await self.get_bytes(path, accept="application/json")
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    async def get_text(self, path: str) -> str:
        body = await self.get_bytes(path, accept="text/plain")
        return body.decode("utf-8", errors="replace")

    async def get_bytes(self, path: str, accept: str = "application/json") -> bytes:
        def _request() -> bytes:
            request = urllib.request.Request(self.url(path), headers={"accept": accept})
            with urllib.request.urlopen(request, timeout=self.config.request_timeout_seconds) as response:
                return response.read()

        try:
            payload = await asyncio.to_thread(_request)
            self.mark_success()
            return payload
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.mark_error(str(exc))
            raise

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
