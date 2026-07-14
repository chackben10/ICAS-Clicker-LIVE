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

    async def presentation_by_uuid(self, uuid: str) -> dict[str, Any]:
        uuid_q = self.client.quote_segment(uuid)
        return await self.client.get_json(f"/presentation/{uuid_q}")

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

    def _active_uuid(self, active_obj: dict[str, Any]) -> str:
        presentation = active_obj.get("presentation")
        if not isinstance(presentation, dict):
            return ""
        presentation_id = presentation.get("id")
        if not isinstance(presentation_id, dict):
            return ""
        return str(presentation_id.get("uuid") or "")

    def _presentation_destination(self, presentation_obj: dict[str, Any]) -> str:
        presentation = presentation_obj.get("presentation")
        if not isinstance(presentation, dict):
            return ""
        return str(presentation.get("destination") or "")

    def _is_blank_preview(self, uuid: str) -> bool:
        blank_uuid = self.config.presentation_behavior.avoid_blank_preview_uuid
        return bool(uuid and blank_uuid and uuid == blank_uuid)

    def _blank_presentation(self, reason: str) -> dict[str, Any]:
        return {"presentation": None, "reason": reason}

    async def _focused_uuid(self) -> str:
        focused = await self.focused_presentation()
        if isinstance(focused.get("uuid"), str):
            return str(focused["uuid"])
        presentation = focused.get("presentation")
        if isinstance(presentation, dict):
            presentation_id = presentation.get("id")
            if isinstance(presentation_id, dict):
                return str(presentation_id.get("uuid") or "")
        return ""

    async def full_presentation(self) -> dict[str, Any]:
        await self.refresh_presentation_base()

        if self._current_base == "/presentation/active":
            active_obj = await self.active_presentation()
            active_uuid = self._active_uuid(active_obj)
            if not self._is_blank_preview(active_uuid):
                return active_obj

        focused_uuid = await self._focused_uuid()
        if not focused_uuid:
            return self._blank_presentation("no_focused")
        if self._is_blank_preview(focused_uuid):
            return self._blank_presentation("blank_preview")

        focused_full = await self.presentation_by_uuid(focused_uuid)
        destination = self._presentation_destination(focused_full)

        if destination == "announcements" and self.config.presentation_behavior.ignore_announcements_focused:
            try:
                await self.client.trigger("/presentation/active/focus")
            except Exception:
                pass
            await asyncio.sleep(self.config.presentation_behavior.refocus_delay_seconds)
            refocused_uuid = await self._focused_uuid()
            if not refocused_uuid or self._is_blank_preview(refocused_uuid):
                return self._blank_presentation("focused_is_announcements")
            refocused_full = await self.presentation_by_uuid(refocused_uuid)
            if self._presentation_destination(refocused_full) == "presentation":
                return refocused_full
            return self._blank_presentation("focused_is_announcements")

        return focused_full

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
        if macro_name == self.config.bible_macro_trigger_uuid:
            return await self.client.trigger_macro(macro_name)
        if not self.macro_allowed(macro_name):
            raise ValueError(f"Macro is not allow-listed: {macro_name}")
        return await self.client.trigger_macro(macro_name)

    async def timer_start(self) -> bool:
        timer_q = self.client.quote_segment(self.config.timer.timer_name)
        return await self.client.trigger(f"/timer/{timer_q}/start")

    async def timer_stop(self) -> bool:
        timer_q = self.client.quote_segment(self.config.timer.timer_name)
        return await self.client.trigger(f"/timer/{timer_q}/stop")

    async def timer_reset(self) -> bool:
        timer_q = self.client.quote_segment(self.config.timer.timer_name)
        return await self.client.trigger(f"/timer/{timer_q}/reset")
