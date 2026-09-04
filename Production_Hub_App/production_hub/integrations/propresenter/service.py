from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from production_hub.core.config.models import ProPresenterConfig
from production_hub.integrations.propresenter.audio_service import ProPresenterAudioService
from production_hub.integrations.propresenter.client import ProPresenterClient, ProPresenterHttpError
from production_hub.integrations.propresenter.status_stream import ProPresenterStatusStream
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
        self._monitor_cache: dict[str, tuple[float, Any]] = {}
        self._monitor_cache_lock = threading.Lock()
        # Auto Show still reacts within half a second, while repeated trigger
        # rules share state instead of opening several API requests per tick.
        self._monitor_cache_seconds = 0.45
        self._stream_cache_seconds = 1.5
        self._status_stream: ProPresenterStatusStream | None = None
        self._status_ingest = None
        # Set by the application so the stream can stop the press-to-cue clock.
        self.press_to_cue: Any | None = None

    @staticmethod
    def _cue_signature(path: str, data: Any) -> str:
        """Identify the cue a status push describes.

        slide_index is the normal path.  Dynamic Bible verses never carry a
        presentation index, so status/slide -- which does report a distinct cue
        UUID per verse -- is the only signal for them.
        """

        if not isinstance(data, dict):
            return ""
        if path == "presentation/slide_index":
            index = data.get("presentation_index")
            if not isinstance(index, dict):
                return ""
            identifier = index.get("presentation_id")
            uuid = identifier.get("uuid") if isinstance(identifier, dict) else ""
            position = index.get("index")
            return f"slide:{uuid}:{position}" if position is not None else ""
        if path == "status/slide":
            for key in ("current", "slide"):
                current = data.get(key)
                if isinstance(current, dict) and current.get("uuid"):
                    return f"cue:{current.get('uuid')}"
            if data.get("uuid"):
                return f"cue:{data.get('uuid')}"
        return ""

    def _cache_monitor_value(self, key: str, value: Any) -> Any:
        with self._monitor_cache_lock:
            self._monitor_cache[key] = (time.monotonic(), value)
        return value

    def _cached_monitor_value(self, key: str, *, max_age: float | None = None) -> Any:
        with self._monitor_cache_lock:
            cached = self._monitor_cache.get(key)
        if cached is None:
            return None
        recorded_at, value = cached
        if max_age is not None and time.monotonic() - recorded_at > max_age:
            return None
        return value

    @property
    def status_stream_connected(self) -> bool:
        return bool(self._status_stream and self._status_stream.connected)

    def start_status_stream(self, callback=None, on_connect=None) -> ProPresenterStatusStream:
        if self._status_stream is not None:
            return self._status_stream

        def ingest(path: str, data: Any) -> None:
            cache_keys = {
                "look/current": "current_look_payload",
                "presentation/slide_index": "slide_index",
                "status/slide": "slide_status",
            }
            cache_key = cache_keys.get(path)
            if cache_key is not None:
                self._cache_monitor_value(cache_key, data if data is not None else {})
            if path == "look/current" and isinstance(data, dict):
                self._cache_monitor_value(
                    "current_look",
                    str(((data.get("id") or {}).get("name")) or "").strip(),
                )
            if self.press_to_cue is not None:
                signature = self._cue_signature(path, data)
                if signature:
                    self.press_to_cue.record_cue(signature)
            if callback is not None:
                callback(path, data)

        self._status_ingest = ingest
        self._status_stream = ProPresenterStatusStream(
            self.config.base_url,
            ingest,
            timeout_seconds=max(12.0, self.config.request_timeout_seconds),
            on_connect=on_connect,
        )
        self._status_stream.start()
        return self._status_stream

    async def rerender_active_slide(self, cue_text: str = "") -> str:
        """Re-trigger the verse that is on screen, so ProPresenter rebuilds it.

        A verse generated while a Song look was active is authored against the
        wrong theme, and changing the look afterwards restyles the text but
        cannot restore the reference -- it was never put in the slide. Only a
        regeneration fixes it, and re-triggering the same cue is a regeneration.

        A dynamic Bible presentation reports no slide index, so the index is
        recovered by matching the live cue's text against the passage. The match
        must be unique: with a repeated verse, or no match at all, this does
        nothing rather than risk jumping the reading to the wrong place.

        Returns a short reason string; "" means it re-triggered.
        """

        wanted = str(cue_text or "").strip()
        try:
            if not wanted:
                # Only re-read when the caller has nothing. /status/slide comes
                # back empty during a transition, which is exactly when this
                # runs, so the observed text is the reliable source.
                live = await self.client.get_json("/status/slide")
                current = live.get("current") if isinstance(live, dict) else None
                wanted = str((current or {}).get("text") or "").strip()
            if not wanted:
                return "no live cue text"

            # The look change that prompted this is still settling, so the
            # first read can arrive before /presentation/active reflects the
            # reading. One short retry covers that without waiting on a verse
            # that genuinely is not there.
            matches: list[int] = []
            for attempt in range(2):
                if attempt:
                    await asyncio.sleep(0.4)
                passage = await self.client.get_json("/presentation/active")
                matches = self._slide_indexes_matching(passage, wanted)
                if matches:
                    break
        except Exception as exc:
            return f"read failed: {exc}"

        if not matches:
            return "live cue not found in the active presentation"
        if len(matches) > 1:
            return f"live cue text appears {len(matches)} times; refusing to guess"

        await self.client.trigger(f"/presentation/active/{matches[0]}/trigger")
        return ""

    @staticmethod
    def _slide_indexes_matching(passage: Any, wanted: str) -> list[int]:
        presentation = passage.get("presentation") if isinstance(passage, dict) else None
        groups = (presentation or {}).get("groups") or []
        matches: list[int] = []
        index = 0
        for group in groups:
            slides = (group.get("slides") or []) if isinstance(group, dict) else []
            for slide in slides:
                if " ".join(str(slide.get("text") or "").split()) == " ".join(wanted.split()):
                    matches.append(index)
                index += 1
        return matches

    async def prime_status_state(self) -> None:
        """Read the values the subscription will never push on its own.

        `/status/updates` only reports changes. A reading already on screen when
        the stream connects -- at startup, or after any reconnect -- produces no
        push at all, so anything tracking state from pushes alone never learns
        it. Reading the three paths once closes that gap.
        """

        if self._status_ingest is None:
            return
        for path, reader in (
            ("look/current", lambda: self.client.get_json("/look/current")),
            ("presentation/current", lambda: self.client.get_json("/presentation/current")),
            ("status/slide", lambda: self.client.get_json("/status/slide")),
        ):
            try:
                self._status_ingest(path, await reader())
            except Exception:
                # A path that cannot be read is not fatal; the next real push
                # will supply it.
                continue

    def stop_status_stream(self) -> None:
        if self._status_stream is not None:
            self._status_stream.stop()
            self._status_stream = None

    def _monitor_max_age(self, *, stream_fed: bool = True) -> float | None:
        # Stream-fed keys can tolerate a slightly longer window because the
        # subscription pushes every change.  Keys the stream does not feed
        # (active_presentation) keep the short window so a quiet cache is
        # refreshed by one shared GET rather than going stale.
        if not stream_fed:
            return self._monitor_cache_seconds
        return (
            self._stream_cache_seconds
            if self.status_stream_connected
            else self._monitor_cache_seconds
        )

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
        data = await self.client.get_json("/presentation/active")
        return self._cache_monitor_value("active_presentation", data)

    async def monitored_active_presentation(self) -> dict[str, Any]:
        cached = self._cached_monitor_value(
            "active_presentation",
            max_age=self._monitor_max_age(stream_fed=False),
        )
        return cached if isinstance(cached, dict) else await self.active_presentation()

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
        data = await self.client.get_json("/presentation/slide_index")
        return self._cache_monitor_value("slide_index", data)

    async def monitored_slide_index(self) -> dict[str, Any]:
        cached = self._cached_monitor_value("slide_index", max_age=self._monitor_max_age())
        return cached if isinstance(cached, dict) else await self.slide_index()

    async def slide_status(self) -> dict[str, Any]:
        """Return the rendered cue when ProPresenter exposes one."""
        data = await self.client.get_json("/status/slide")
        return self._cache_monitor_value("slide_status", data)

    async def monitored_slide_status(self) -> dict[str, Any]:
        cached = self._cached_monitor_value("slide_status", max_age=self._monitor_max_age())
        return cached if isinstance(cached, dict) else await self.slide_status()

    async def current_look_name(self) -> str:
        data = await self.client.get_json("/look/current")
        look = str(((data.get("id") or {}).get("name")) or "").strip()
        return self._cache_monitor_value("current_look", look)

    async def monitored_current_look_name(self) -> str:
        cached = self._cached_monitor_value("current_look", max_age=self._monitor_max_age())
        return str(cached) if cached is not None else await self.current_look_name()

    async def refresh_presentation_base(self) -> str:
        try:
            return self._base_from_slide_index(await self.slide_index())
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

    def _base_from_slide_index(self, data: dict[str, Any]) -> str:
        index = (data.get("presentation_index") or {}).get("index") if isinstance(data, dict) else None
        self._current_base = "/presentation/active" if index is not None else "/presentation/focused"
        return self._current_base

    async def _fast_presentation_base(self) -> str:
        """Decide active-vs-focused from the stream cache, without a blocking GET."""

        try:
            return self._base_from_slide_index(await self.monitored_slide_index())
        except Exception:
            # A cold or failed cache is not worth delaying a keypress for; the
            # last known base is retried and corrected below if it was wrong.
            return self._current_base

    async def _advance(self, verb: str) -> bool:
        """Fire on the cached base, then correct once if the cache was stale.

        A clicker press used to cost two round-trips: an uncached slide_index
        GET to choose the base, then the trigger.  The status stream already
        pushes slide_index, so the first request is usually avoidable entirely.
        """

        base = await self._fast_presentation_base()
        try:
            return await self.client.trigger(f"{base}/{verb}/trigger")
        except ProPresenterHttpError:
            # ProPresenter rejected the cached base. Re-read it authoritatively
            # and retry once. A transport failure is not retried here: the
            # second request would fail the same way.
            corrected = await self.refresh_presentation_base()
            if corrected == base:
                raise
            return await self.client.trigger(f"{corrected}/{verb}/trigger")

    async def next_slide(self) -> bool:
        return await self._advance("next")

    async def previous_slide(self) -> bool:
        return await self._advance("previous")

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
        loaded = False
        while loop.time() < deadline:
            remaining = deadline - loop.time()
            try:
                loaded = await asyncio.wait_for(
                    self._target_loaded(presentation_uuid, playlist_uuid, item_index),
                    timeout=max(0.01, remaining),
                )
            except (TimeoutError, asyncio.TimeoutError):
                break
            remaining = deadline - loop.time()
            if loaded or remaining <= 0:
                break
            await asyncio.sleep(min(self.PRESENTATION_SWITCH_POLL_SECONDS, remaining))

        # Loading/focus can be reported before ProPresenter has finished executing
        # the disabled setup slide's actions. Preserve the full settling window so
        # macros, audio, looks, and stage actions can run before the requested cue.
        remaining = deadline - loop.time()
        if remaining > 0:
            await asyncio.sleep(remaining)

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
