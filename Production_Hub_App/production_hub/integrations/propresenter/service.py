from __future__ import annotations

import asyncio
from typing import Any

from production_hub.core.config.models import ProPresenterConfig
from production_hub.integrations.propresenter.audio_service import ProPresenterAudioService
from production_hub.integrations.propresenter.client import ProPresenterClient
from production_hub.integrations.propresenter.thumbnail_cache import ThumbnailCache


class ProPresenterService:
    def __init__(self, config: ProPresenterConfig) -> None:
        self.config = config
        self.client = ProPresenterClient(config)
        self.audio = ProPresenterAudioService(self.client, config.audio)
        self.thumbnails = ThumbnailCache(self.client, config.thumbnails)
        self._current_base = "/presentation/focused"

    def _presentation_uuid(self, label: str) -> str:
        for item in self.config.presentations:
            if item.label == label:
                return item.uuid
        raise ValueError(f"Unknown presentation mapping: {label}")

    def service_logo_uuid(self, name_or_uuid: str) -> str:
        for item in self.config.service_logos:
            if item.name == name_or_uuid or item.uuid == name_or_uuid:
                return item.uuid
        raise ValueError(f"Unknown service logo: {name_or_uuid}")

    def macro_allowed(self, macro_name: str) -> bool:
        return macro_name in {macro.macro_name for macro in self.config.macros}

    async def health_check(self) -> bool:
        await self.client.get_json("/presentation/slide_index")
        return True

    async def active_presentation(self) -> dict[str, Any]:
        return await self.client.get_json("/presentation/active")

    async def focused_presentation(self) -> dict[str, Any]:
        return await self.client.get_json("/presentation/focused")

    async def slide_index(self) -> dict[str, Any]:
        return await self.client.get_json("/presentation/slide_index")

    async def current_look_name(self) -> str:
        data = await self.client.get_json("/look/current")
        return str(((data.get("id") or {}).get("name")) or "").strip()

    async def refresh_presentation_base(self) -> str:
        try:
            data = await self.slide_index()
            if (data.get("presentation_index") or {}).get("index") is not None:
                self._current_base = "/presentation/active"
            else:
                self._current_base = "/presentation/focused"
        except Exception:
            self._current_base = "/presentation/focused"
        return self._current_base

    async def next_slide(self) -> bool:
        await self.refresh_presentation_base()
        return await self.client.trigger(f"{self._current_base}/next/trigger")

    async def previous_slide(self) -> bool:
        await self.refresh_presentation_base()
        return await self.client.trigger(f"{self._current_base}/previous/trigger")

    async def focus_slide(self, index: int) -> bool:
        await self.refresh_presentation_base()
        return await self.client.trigger(f"{self._current_base}/{int(index)}/trigger")

    async def trigger_presentation_label(self, label: str) -> bool:
        return await self.client.trigger_presentation(self._presentation_uuid(label))

    async def trigger_presentation_uuid(self, uuid: str) -> bool:
        return await self.client.trigger_presentation(uuid)

    async def trigger_service_logo(self, name_or_uuid: str) -> bool:
        return await self.client.trigger_presentation(self.service_logo_uuid(name_or_uuid))

    async def clear_announcements(self) -> bool:
        return await self.client.trigger("/clear/layer/announcements")

    async def clear_slide(self, delay_seconds: float = 0) -> bool:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        return await self.client.trigger("/clear/layer/slide")

    async def trigger_macro(self, macro_name: str) -> bool:
        if not self.macro_allowed(macro_name):
            raise ValueError(f"Macro is not allow-listed: {macro_name}")
        return await self.client.trigger_macro(macro_name)

    async def timer_start(self) -> bool:
        return await self.client.trigger(f"/timer/{self.config.timer.timer_name}/start")

    async def timer_stop(self) -> bool:
        return await self.client.trigger(f"/timer/{self.config.timer.timer_name}/stop")

    async def timer_reset(self) -> bool:
        return await self.client.trigger(f"/timer/{self.config.timer.timer_name}/reset")

