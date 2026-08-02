from __future__ import annotations

import asyncio
from typing import Any

from production_hub.core.config.models import ProPresenterConfig
from production_hub.integrations.propresenter.audio_service import ProPresenterAudioService
from production_hub.integrations.propresenter.client import ProPresenterClient
from production_hub.integrations.propresenter.thumbnail_cache import ThumbnailCache


class ProPresenterService:
    PRESENTATION_SWITCH_POLL_SECONDS = 0.1
    PRESENTATION_SWITCH_WAIT_SECONDS = 1.0

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
        uuid = str(uuid or "").strip()
        if not uuid:
            raise ValueError("Presentation UUID is required")
        uuid_q = self.client.quote_segment(uuid)
        return await self.client.get_json(f"/presentation/{uuid_q}")

    async def focused_playlist(self) -> dict[str, Any]:
        return await self.client.get_json("/playlist/focused")

    async def playlist_by_uuid(self, uuid: str) -> dict[str, Any]:
        uuid = str(uuid or "").strip()
        if not uuid:
            raise ValueError("Playlist UUID is required")
        uuid_q = self.client.quote_segment(uuid)
        return await self.client.get_json(f"/playlist/{uuid_q}")

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

    async def trigger_presentation_slide(self, uuid: str, index: int) -> bool:
        uuid = str(uuid or "").strip()
        if not uuid:
            raise ValueError("Presentation UUID is required")
        index = int(index)
        if index < 0:
            raise ValueError("Slide index must be nonnegative")

        playlist_target = await self._focused_playlist_target_for_presentation(uuid)
        if playlist_target is not None:
            playlist_uuid, item_index = playlist_target
            return await self._switch_presentation_slide(
                presentation_uuid=uuid,
                slide_index=index,
                playlist_uuid=playlist_uuid,
                item_index=item_index,
            )

        return await self._switch_presentation_slide(
            presentation_uuid=uuid,
            slide_index=index,
        )

    async def trigger_playlist_presentation_slide(
        self,
        playlist_uuid: str,
        item_index: int,
        slide_index: int,
    ) -> bool:
        playlist_uuid = str(playlist_uuid or "").strip()
        if not playlist_uuid:
            raise ValueError("Playlist UUID is required")
        item_index = int(item_index)
        slide_index = int(slide_index)
        if item_index < 0:
            raise ValueError("Playlist item index must be nonnegative")
        if slide_index < 0:
            raise ValueError("Slide index must be nonnegative")

        playlist = await self.playlist_by_uuid(playlist_uuid)
        item = self._playlist_item_at_index(playlist, item_index)
        if not self._is_allowed_playlist_presentation(item):
            raise ValueError("Playlist item is not an allowed presentation")
        presentation_uuid = self._playlist_presentation_uuid(item)
        if not presentation_uuid:
            raise ValueError("Playlist presentation UUID is required")

        return await self._switch_presentation_slide(
            presentation_uuid=presentation_uuid,
            slide_index=slide_index,
            playlist_uuid=playlist_uuid,
            item_index=item_index,
        )

    @staticmethod
    def _focused_playlist_identity(payload: dict[str, Any]) -> tuple[str, int | None]:
        playlist = payload.get("playlist")
        item = payload.get("item")
        playlist = playlist if isinstance(playlist, dict) else {}
        item = item if isinstance(item, dict) else {}
        playlist_uuid = str(playlist.get("uuid") or "").strip()
        raw_index = item.get("index")
        try:
            item_index = int(raw_index) if raw_index is not None else None
        except (TypeError, ValueError):
            item_index = None
        return playlist_uuid, item_index

    @staticmethod
    def _playlist_item_index(item: dict[str, Any]) -> int | None:
        identifier = item.get("id")
        identifier = identifier if isinstance(identifier, dict) else {}
        raw_index = identifier.get("index")
        try:
            return int(raw_index) if raw_index is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _playlist_presentation_uuid(item: dict[str, Any]) -> str:
        info = item.get("presentation_info")
        info = info if isinstance(info, dict) else {}
        return str(info.get("presentation_uuid") or "").strip()

    @classmethod
    def _is_allowed_playlist_presentation(cls, item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        return (
            item.get("type") == "presentation"
            and item.get("destination") == "presentation"
            and item.get("is_hidden") is False
            and bool(cls._playlist_presentation_uuid(item))
        )

    @classmethod
    def _playlist_item_at_index(
        cls,
        playlist: dict[str, Any],
        item_index: int,
    ) -> dict[str, Any] | None:
        items = playlist.get("items")
        if not isinstance(items, list):
            return None
        for item in items:
            if isinstance(item, dict) and cls._playlist_item_index(item) == item_index:
                return item
        return None

    async def _focused_playlist_target_for_presentation(
        self,
        presentation_uuid: str,
    ) -> tuple[str, int] | None:
        try:
            focused = await self.focused_playlist()
        except Exception:
            return None
        playlist_uuid, focused_index = self._focused_playlist_identity(focused)
        if not playlist_uuid or focused_index is None:
            return None
        playlist = await self.playlist_by_uuid(playlist_uuid)

        requested = presentation_uuid.casefold()
        matches: list[int] = []
        for item in playlist.get("items") or []:
            if not self._is_allowed_playlist_presentation(item):
                continue
            if self._playlist_presentation_uuid(item).casefold() != requested:
                continue
            item_index = self._playlist_item_index(item)
            if item_index is not None:
                matches.append(item_index)

        if not matches:
            return None
        matches.sort()
        next_index = next((candidate for candidate in matches if candidate >= focused_index), None)
        return playlist_uuid, next_index if next_index is not None else matches[-1]

    @staticmethod
    def _first_slide_disabled(presentation_payload: dict[str, Any]) -> bool:
        presentation = presentation_payload.get("presentation")
        presentation = presentation if isinstance(presentation, dict) else {}
        for group in presentation.get("groups") or []:
            if not isinstance(group, dict):
                continue
            slides = group.get("slides")
            if not isinstance(slides, list) or not slides:
                continue
            first = slides[0]
            return isinstance(first, dict) and first.get("enabled") is False
        return False

    async def _raw_trigger_presentation_slide(self, uuid: str, index: int) -> bool:
        uuid_q = self.client.quote_segment(uuid)
        return await self.client.trigger(f"/presentation/{uuid_q}/{int(index)}/trigger")

    async def _raw_trigger_playlist_slide(
        self,
        playlist_uuid: str,
        item_index: int,
        slide_index: int,
    ) -> bool:
        playlist_q = self.client.quote_segment(playlist_uuid)
        return await self.client.trigger(
            f"/playlist/{playlist_q}/{int(item_index)}/{int(slide_index)}/trigger"
        )

    async def _target_is_current(
        self,
        presentation_uuid: str,
        playlist_uuid: str | None,
        item_index: int | None,
    ) -> bool:
        if playlist_uuid is not None and item_index is not None:
            try:
                focused = await self.focused_playlist()
                focused_uuid, focused_index = self._focused_playlist_identity(focused)
                return focused_uuid.casefold() == playlist_uuid.casefold() and focused_index == item_index
            except Exception:
                return False

        active_result, focused_result = await asyncio.gather(
            self.active_presentation(),
            self.focused_presentation(),
            return_exceptions=True,
        )
        active = active_result if isinstance(active_result, dict) else {}
        focused = focused_result if isinstance(focused_result, dict) else {}
        return presentation_uuid.casefold() in {
            self._active_uuid(active).casefold(),
            self._focused_uuid_from_payload(focused).casefold(),
        }

    @staticmethod
    def _focused_uuid_from_payload(payload: dict[str, Any]) -> str:
        if isinstance(payload.get("uuid"), str):
            return str(payload["uuid"])
        presentation = payload.get("presentation")
        presentation = presentation if isinstance(presentation, dict) else {}
        identifier = presentation.get("id")
        identifier = identifier if isinstance(identifier, dict) else {}
        return str(identifier.get("uuid") or "")

    async def _target_loaded(
        self,
        presentation_uuid: str,
        playlist_uuid: str | None,
        item_index: int | None,
    ) -> bool:
        checks = [self.active_presentation()]
        if playlist_uuid is not None and item_index is not None:
            checks.append(self.focused_playlist())
        else:
            checks.append(self.focused_presentation())
        results = await asyncio.gather(*checks, return_exceptions=True)

        active = results[0] if isinstance(results[0], dict) else {}
        if self._active_uuid(active).casefold() == presentation_uuid.casefold():
            return True
        focused = results[1] if isinstance(results[1], dict) else {}
        if playlist_uuid is not None and item_index is not None:
            focused_uuid, focused_index = self._focused_playlist_identity(focused)
            return focused_uuid.casefold() == playlist_uuid.casefold() and focused_index == item_index
        return self._focused_uuid_from_payload(focused).casefold() == presentation_uuid.casefold()

    async def _wait_for_target_load(
        self,
        presentation_uuid: str,
        playlist_uuid: str | None,
        item_index: int | None,
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.PRESENTATION_SWITCH_WAIT_SECONDS
        while loop.time() < deadline:
            remaining = deadline - loop.time()
            try:
                if await asyncio.wait_for(
                    self._target_loaded(presentation_uuid, playlist_uuid, item_index),
                    timeout=max(0.01, remaining),
                ):
                    return
            except (TimeoutError, asyncio.TimeoutError):
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(self.PRESENTATION_SWITCH_POLL_SECONDS, remaining))

    async def _switch_presentation_slide(
        self,
        presentation_uuid: str,
        slide_index: int,
        playlist_uuid: str | None = None,
        item_index: int | None = None,
    ) -> bool:
        if playlist_uuid is not None and item_index is not None:
            trigger = lambda index: self._raw_trigger_playlist_slide(playlist_uuid, item_index, index)
        else:
            trigger = lambda index: self._raw_trigger_presentation_slide(presentation_uuid, index)

        if await self._target_is_current(presentation_uuid, playlist_uuid, item_index):
            return await trigger(slide_index)

        presentation = await self.presentation_by_uuid(presentation_uuid)
        if slide_index != 0 and self._first_slide_disabled(presentation):
            await trigger(0)
            await self._wait_for_target_load(presentation_uuid, playlist_uuid, item_index)

        return await trigger(slide_index)

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
