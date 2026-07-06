from __future__ import annotations

import re
import time

from production_hub.core.config.models import AudioConfig
from production_hub.integrations.propresenter.client import ProPresenterClient
from production_hub.integrations.propresenter.models import AudioTrack


EXTENSION_RE = re.compile(r"\.(wav|mp3|aiff|aif|m4a)$", re.IGNORECASE)


def strip_audio_extension(name: str) -> str:
    return EXTENSION_RE.sub("", str(name or "").strip()).strip()


def normalize_audio_name(name: str) -> str:
    return re.sub(r"\s+", "", strip_audio_extension(name)).lower()


def normalize_pad_key(name: str) -> str:
    value = strip_audio_extension(name)
    value = re.sub(r"\s*\((major|minor|nuetral|neutral)\)\s*", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(major|minor|nuetral|neutral|pads?)\b", " ", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", "", value).lower()


class ProPresenterAudioService:
    def __init__(self, client: ProPresenterClient, config: AudioConfig) -> None:
        self.client = client
        self.config = config
        self._cache: dict[str, tuple[float, list[str]]] = {}
        self._history: list[str] = []
        self._history_set: set[str] = set()

    async def playlists(self) -> list[str]:
        return list(self.config.playlists)

    async def playlist_tracks(self, playlist: str) -> list[str]:
        cached = self._cache.get(playlist)
        if cached and (time.time() - cached[0]) < self.config.cache_ttl_seconds:
            return cached[1]
        playlist_q = self.client.quote_segment(playlist)
        data = await self.client.get_json(f"/audio/playlist/{playlist_q}")
        tracks: list[str] = []
        for item in data.get("items", []):
            name = ""
            if isinstance(item, dict):
                item_id = item.get("id")
                if isinstance(item_id, dict):
                    name = str(item_id.get("name") or "")
                name = name or str(item.get("name") or "")
            if name.strip():
                tracks.append(name.strip())
        self._cache[playlist] = (time.time(), tracks)
        return tracks

    async def find_track(self, label: str) -> AudioTrack | None:
        wanted = normalize_audio_name(label)
        if not wanted:
            return None
        for playlist in self.config.playlists:
            for track in await self.playlist_tracks(playlist):
                if normalize_audio_name(track) == wanted:
                    return AudioTrack(playlist, track)
        return None

    async def find_track_in_playlist(self, playlist: str, label: str) -> AudioTrack | None:
        wanted = normalize_audio_name(label)
        wanted_pad_key = normalize_pad_key(label)
        if not playlist or not wanted:
            return None
        for track in await self.playlist_tracks(playlist):
            if normalize_audio_name(track) == wanted:
                return AudioTrack(playlist, track)
        if wanted_pad_key:
            for track in await self.playlist_tracks(playlist):
                if normalize_pad_key(track) == wanted_pad_key:
                    return AudioTrack(playlist, track)
        return None

    def remember_triggered(self, key: str) -> bool:
        if not key or not self.config.prevent_duplicate_triggers:
            return True
        if key in self._history_set:
            return False
        self._history.append(key)
        self._history_set.add(key)
        while len(self._history) > self.config.history_max:
            old = self._history.pop(0)
            self._history_set.discard(old)
        return True

    async def trigger(self, playlist: str, track: str) -> bool:
        return await self.client.trigger_audio(playlist, track)

    async def clear(self) -> bool:
        return await self.client.trigger("/clear/layer/audio")

    async def active_text(self) -> str:
        try:
            data = await self.client.get_json("/audio/playlist/active")
        except Exception:
            return "No audio playing\n"
        playlist = (data.get("playlist") or {}).get("name")
        item = (data.get("item") or {}).get("name")
        if not playlist or not item:
            return "No audio playing\n"
        return f"playlist: {playlist}\nitem: {item}\n"
