from __future__ import annotations

import asyncio
import io
import time
from dataclasses import dataclass

from PIL import Image

from production_hub.core.config.models import ThumbnailConfig
from production_hub.integrations.propresenter.client import ProPresenterClient

TRANSPARENT_PNG = bytes.fromhex(
    "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C489"
    "0000000A49444154789C63000100000500010D0A2DB40000000049454E44AE426082"
)


@dataclass
class ThumbnailEntry:
    body: bytes
    content_type: str
    stored_at: float
    tier: str


class ThumbnailCache:
    def __init__(self, client: ProPresenterClient, config: ThumbnailConfig) -> None:
        self.client = client
        self.config = config
        self._cache: dict[tuple[str, int, str], ThumbnailEntry] = {}
        self._inflight: dict[tuple, asyncio.Task] = {}
        self._limits: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}

    def _ttl(self, tier: str) -> float:
        return self.config.high_cache_ttl_seconds if tier == "high" else self.config.low_cache_ttl_seconds

    def get(self, uuid: str, index: int, tier: str = "low") -> ThumbnailEntry | None:
        entry = self._cache.get((uuid, index, tier))
        if not entry:
            return None
        if (time.time() - entry.stored_at) > self._ttl(tier):
            return None
        return entry

    def put(self, uuid: str, index: int, body: bytes, content_type: str, tier: str) -> ThumbnailEntry:
        while len(self._cache) >= self.config.max_cache_items:
            oldest = min(self._cache, key=lambda key: self._cache[key].stored_at)
            self._cache.pop(oldest, None)
        entry = ThumbnailEntry(body, content_type, time.time(), tier)
        self._cache[(uuid, index, tier)] = entry
        return entry

    async def fetch(self, uuid: str, index: int, tier: str = "low") -> ThumbnailEntry:
        cached = self.get(uuid, index, tier)
        if cached:
            return cached
        loop = asyncio.get_running_loop()
        key = (loop, uuid, index, tier)
        task = self._inflight.get(key)
        if task is None:
            task = loop.create_task(self._fetch(uuid, index, tier))
            self._inflight[key] = task
            task.add_done_callback(lambda done: self._inflight.pop(key, None))
        return await asyncio.shield(task)

    async def _fetch(self, uuid: str, index: int, tier: str) -> ThumbnailEntry:
        loop = asyncio.get_running_loop()
        limit = self._limits.setdefault(loop, asyncio.Semaphore(3))
        quality = self.config.high_quality if tier == "high" else self.config.low_quality
        uuid_q = self.client.quote_segment(uuid)
        format_q = self.client.quote_segment(self.config.image_format)
        path = f"/presentation/{uuid_q}/thumbnail/{index}?quality={quality}&thumbnail_type={format_q}"
        try:
            async with limit:
                body = await self.client.get_bytes(path, accept=f"image/{self.config.image_format}")
            # A 200 response may still be an empty/transparent placeholder or
            # truncated image. Do not turn it into a successful cached preview.
            with Image.open(io.BytesIO(body)) as image:
                image.load()
                if image.width <= 1 or image.height <= 1:
                    raise ValueError("Thumbnail is a placeholder")
                if image.mode == "RGBA" and image.getchannel("A").getextrema()[1] == 0:
                    raise ValueError("Thumbnail is transparent")
        except Exception:
            stale = self._cache.get((uuid, index, tier))
            if stale and time.time() - stale.stored_at <= self.config.high_cache_ttl_seconds:
                return stale
            return ThumbnailEntry(TRANSPARENT_PNG, "image/png", time.time(), tier)
        return self.put(uuid, index, body, f"image/{self.config.image_format}", tier)
