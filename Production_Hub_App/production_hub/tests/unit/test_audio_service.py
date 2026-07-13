from __future__ import annotations

import unittest

from production_hub.core.config.models import AudioConfig
from production_hub.integrations.propresenter.audio_service import ProPresenterAudioService
from production_hub.integrations.propresenter.audio_service import normalize_audio_name, strip_audio_extension


class FakeAudioClient:
    def __init__(self, payload):
        self.payload = payload
        self.paths = []

    async def get_json(self, path: str):
        self.paths.append(path)
        return dict(self.payload)


class AudioServiceTests(unittest.TestCase):
    def test_audio_name_normalization_matches_hammerspoon_behavior(self) -> None:
        self.assertEqual(strip_audio_extension("D(Major).wav"), "D(Major)")
        self.assertEqual(normalize_audio_name(" D (Major).WAV "), "d(major)")
        self.assertEqual(normalize_audio_name("Pad Song.m4a"), "padsong")


class AudioServiceAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_text_uses_transport_audio_current_and_strips_extension(self) -> None:
        client = FakeAudioClient(
            {
                "is_playing": True,
                "uuid": "4B807B51-6EC3-4F13-9A1D-2509738DFC45",
                "name": "C(Major).wav",
                "artist": "unknown",
                "audio_only": True,
                "duration": 60.0,
            }
        )
        service = ProPresenterAudioService(client, AudioConfig())
        self.assertEqual(await service.active_text(), "C(Major)")
        self.assertEqual(client.paths, ["/transport/audio/current"])

    async def test_active_text_returns_blank_when_audio_is_not_playing(self) -> None:
        client = FakeAudioClient(
            {
                "is_playing": False,
                "uuid": "",
                "name": "",
                "artist": "",
                "audio_only": True,
                "duration": 0.0,
            }
        )
        service = ProPresenterAudioService(client, AudioConfig())
        self.assertEqual(await service.active_text(), "")


if __name__ == "__main__":
    unittest.main()
