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
        # The last slide position ProPresenter actually reported, how many
        # slide_index pushes the stream had seen then, and the clicker presses
        # sent since. See _fill_missing_position.
        self._position_events = 0
        self._known_position: dict[str, Any] | None = None
        self._pending_press: dict[str, Any] | None = None

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
            if path == "presentation/slide_index":
                self._note_position(data, pushed=True)
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
        try:
            payload = await self.client.get_json(f"/presentation/{uuid_q}")
        except ProPresenterHttpError as exc:
            if exc.code != 404:
                raise
            # Some migrated ProPresenter 22 documents are readable as current
            # but are missing from the UUID index. Never substitute an unrelated
            # current document (in particular, its sermon notes).
            payload = await self.client.get_json("/presentation/current")
            if self._active_uuid(payload).casefold() != uuid.casefold():
                raise exc
        if self._active_uuid(payload).casefold() != uuid.casefold():
            raise ValueError("ProPresenter returned a different presentation than requested")
        return payload

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
            data = await self.slide_index()
            playlist_uuid = await self._active_playlist_uuid() if not self._has_slide_index(data) else ""
            return self._base_from_slide_index(data, playlist_uuid=playlist_uuid)
        except Exception:
            # A timeout does not mean the selected preview became live.
            pass
        return self._current_base

    @staticmethod
    def _has_slide_index(data: dict[str, Any]) -> bool:
        return isinstance(data, dict) and (data.get("presentation_index") or {}).get("index") is not None

    async def _active_playlist_uuid(self, *, cached: bool = False) -> str:
        if cached:
            value = self._cached_monitor_value("active_playlist_uuid", max_age=self._monitor_cache_seconds)
            if isinstance(value, str):
                return value
        data = await self.client.get_json("/playlist/active")
        active = data.get("presentation") or {}
        item = active.get("playlist_item") or {}
        uuid = self._playlist_presentation_uuid(item)
        self._cache_monitor_value("active_playlist_identity", {
            "uuid": uuid, "name": str((item.get("id") or {}).get("name") or ""),
        })
        return self._cache_monitor_value("active_playlist_uuid", uuid)

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

    @staticmethod
    def _position_fits(data: dict[str, Any], payload: dict[str, Any]) -> bool:
        position = (data.get("presentation_index") or {}) if isinstance(data, dict) else {}
        presentation = payload.get("presentation")
        if position.get("index") is None or not isinstance(presentation, dict):
            return True
        slides = sum(len(group.get("slides") or []) for group in presentation.get("groups") or [])
        total = position.get("total_cues")
        try:
            return int(position["index"]) < slides and (total is None or int(total) <= slides)
        except (TypeError, ValueError):
            return False

    def _note_position(self, data: Any, *, pushed: bool) -> None:
        if pushed:
            self._position_events += 1
        position = data.get("presentation_index") if isinstance(data, dict) else None
        if not isinstance(position, dict) or position.get("index") is None:
            return
        try:
            index = int(position["index"])
        except (TypeError, ValueError):
            return
        uuid = str((position.get("presentation_id") or {}).get("uuid") or "")
        known = self._known_position
        if (known and known["uuid"] == uuid and known["index"] == index
                and known["events"] == self._position_events):
            return
        self._known_position = {"uuid": uuid, "index": index, "events": self._position_events}

    def _record_press(self, step: int, known_before: dict[str, Any] | None) -> None:
        known = self._known_position
        pending = self._pending_press
        if known is None or known is not known_before:
            # ProPresenter reported a position while the trigger was in
            # flight: this press was reported, so there is nothing to infer.
            self._pending_press = None
        elif pending is not None and pending["known"] is known:
            self._pending_press = {**pending, "step": pending["step"] + step}
        else:
            self._pending_press = {"known": known, "index": known["index"], "step": step}

    def _record_jump(self, index: int, known_before: dict[str, Any] | None) -> None:
        known = self._known_position
        self._pending_press = (
            {"known": known, "index": index, "step": 0} if known and known is known_before else None
        )

    def _fill_missing_position(self, after: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        """Supply a live position ProPresenter 22 left out.

        Two different gaps. A GET of slide_index decays to null about 12 s
        after a cue while the stream pushes nothing: the last reported
        position is still the real one. And after an idle pause a cue can be
        pushed with no position at all: if a clicker press caused it, step
        from the last reported position and mark the result as estimated.
        Any later report from ProPresenter replaces both.
        """

        uuid = self._active_uuid(payload)
        known = self._known_position
        if not uuid or not known or known["uuid"].casefold() != uuid.casefold():
            return after
        pending = self._pending_press
        if pending is not None and pending["known"] is known:
            index = self._step_enabled(payload, pending["index"], pending["step"])
            estimated = True
        elif self.status_stream_connected and self._position_events == known["events"]:
            index, estimated = known["index"], False
        else:
            return after
        if index is None:
            return after
        position: dict[str, Any] = {"index": index, "presentation_id": {"uuid": uuid}}
        if estimated:
            position["estimated"] = True
        return {**after, "presentation_index": position}

    @staticmethod
    def _step_enabled(payload: dict[str, Any], start: int, step: int) -> int | None:
        presentation = payload.get("presentation")
        if not isinstance(presentation, dict):
            return None
        slides = [slide for group in presentation.get("groups") or [] for slide in group.get("slides") or []]
        if not 0 <= start < len(slides):
            return None
        index = start
        direction = 1 if step > 0 else -1
        for _ in range(abs(step)):
            candidate = index + direction
            # ProPresenter's next/previous skip disabled slides.
            while 0 <= candidate < len(slides) and (slides[candidate] or {}).get("enabled") is False:
                candidate += direction
            if not 0 <= candidate < len(slides):
                break
            index = candidate
        return index

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
        # A failed live-state read is not evidence that the operator's focused
        # document is live. Let the browser retain its last confirmed content.
        # Bracket the document read so a cue change cannot mix one document's
        # notes with another document's slide number.
        for _ in range(2):
            before = await self.slide_index()
            position = before.get("presentation_index") or {}
            playlist_uuid = await self._active_playlist_uuid() if not self._has_slide_index(before) else ""
            live = position.get("index") is not None or bool(playlist_uuid)
            focused_uuid = ""
            if live:
                expected = str((position.get("presentation_id") or {}).get("uuid") or playlist_uuid)
                payload = await self.active_presentation()
                if expected and self._active_uuid(payload).casefold() != expected.casefold():
                    payload = await self.presentation_by_uuid(expected)
            else:
                focused_uuid = await self._focused_uuid()
                if not focused_uuid or self._is_blank_preview(focused_uuid):
                    payload = self._blank_presentation("no_focused" if not focused_uuid else "blank_preview")
                else:
                    payload = await self.presentation_by_uuid(focused_uuid)
                    if (self._presentation_destination(payload) == "announcements"
                            and self.config.presentation_behavior.ignore_announcements_focused):
                        # Reading the clicker must not change ProPresenter's focus.
                        payload = self._blank_presentation("focused_is_announcements")
            after = await self.slide_index()
            if before.get("presentation_index") != after.get("presentation_index"):
                continue
            if not self._has_slide_index(before) and playlist_uuid != await self._active_playlist_uuid():
                continue
            if not live and focused_uuid != await self._focused_uuid():
                continue
            if self._is_blank_preview(self._active_uuid(payload)):
                payload = self._blank_presentation("blank_preview")
            presentation = payload.get("presentation")
            if isinstance(presentation, dict):
                identity = presentation.get("id") or {}
                playlist_identity = self._cached_monitor_value("active_playlist_identity") or {}
                if not identity.get("name") and live and not playlist_uuid:
                    # ProPresenter 22 can report a live document with a blank
                    # name; its playlist entry still carries the real name.
                    try:
                        await self._active_playlist_uuid()
                        playlist_identity = self._cached_monitor_value("active_playlist_identity") or {}
                    except Exception:
                        pass
                if (not identity.get("name") and self._active_uuid(payload)
                        and str(playlist_identity.get("uuid", "")).casefold() == self._active_uuid(payload).casefold()):
                    payload = {**payload, "presentation": {**presentation, "id": {
                        **identity, "name": playlist_identity.get("name", ""),
                    }}}
            self._base_from_slide_index(after, playlist_uuid=playlist_uuid)
            if not self._position_fits(after, payload):
                # Right after a playlist switch ProPresenter 22 can label the
                # previous document's position with the new document's UUID.
                after = {**after, "presentation_index": None}
            if self._has_slide_index(after):
                self._note_position(after, pushed=False)
            elif live:
                after = self._fill_missing_position(after, payload)
            return {
                **payload,
                "slide_index": after,
                "presentation_mode": "live" if live else "focused",
                "position_estimated": bool((after.get("presentation_index") or {}).get("estimated")),
            }
        raise RuntimeError("ProPresenter changed presentations while reading; retry the snapshot")

    def _base_from_slide_index(self, data: dict[str, Any], *, playlist_uuid: str = "") -> str:
        index = (data.get("presentation_index") or {}).get("index") if isinstance(data, dict) else None
        self._current_base = "/presentation/active" if index is not None or playlist_uuid else "/presentation/focused"
        return self._current_base

    async def _fast_presentation_base(self) -> str:
        """Decide active-vs-focused from the stream cache, without a blocking GET."""

        try:
            data = await self.monitored_slide_index()
            playlist_uuid = await self._active_playlist_uuid(cached=True) if not self._has_slide_index(data) else ""
            return self._base_from_slide_index(data, playlist_uuid=playlist_uuid)
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
        known_before = self._known_position
        try:
            result = await self.client.trigger(f"{base}/{verb}/trigger")
        except ProPresenterHttpError:
            # ProPresenter rejected the cached base. Re-read it authoritatively
            # and retry once. A transport failure is not retried here: the
            # second request would fail the same way.
            corrected = await self.refresh_presentation_base()
            if corrected == base:
                raise
            result = await self.client.trigger(f"{corrected}/{verb}/trigger")
        self._record_press(1 if verb == "next" else -1, known_before)
        return result

    async def next_slide(self) -> bool:
        return await self._advance("next")

    async def previous_slide(self) -> bool:
        return await self._advance("previous")

    async def focus_slide(self, index: int) -> bool:
        await self.refresh_presentation_base()
        known_before = self._known_position
        result = await self.client.trigger(f"{self._current_base}/{int(index)}/trigger")
        self._record_jump(int(index), known_before)
        return result

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
