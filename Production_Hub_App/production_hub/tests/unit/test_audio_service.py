from __future__ import annotations

import unittest

from production_hub.integrations.propresenter.audio_service import normalize_audio_name, strip_audio_extension


class AudioServiceTests(unittest.TestCase):
    def test_audio_name_normalization_matches_hammerspoon_behavior(self) -> None:
        self.assertEqual(strip_audio_extension("D(Major).wav"), "D(Major)")
        self.assertEqual(normalize_audio_name(" D (Major).WAV "), "d(major)")
        self.assertEqual(normalize_audio_name("Pad Song.m4a"), "padsong")


if __name__ == "__main__":
    unittest.main()

