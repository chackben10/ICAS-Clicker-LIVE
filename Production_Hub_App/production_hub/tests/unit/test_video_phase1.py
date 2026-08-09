from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from production_hub.core.config.models import AppConfig
from production_hub.video.frame_broker import LatestFrameBroker
from production_hub.video.models import VideoSourceKey, VideoSourceState
from production_hub.video.ndi_source import AudienceNDISource, NDISourceSettings
from production_hub.video.recording import DiagnosticRecorder, ReplayVideoSource


APP = QApplication.instance() or QApplication([])


def image(color: str = "#244268") -> QImage:
    value = QImage(320, 180, QImage.Format.Format_RGB32)
    value.fill(QColor(color))
    return value


class VideoPhaseOneTests(unittest.TestCase):
    def test_old_config_profiles_receive_safe_video_defaults(self) -> None:
        config = AppConfig.from_dict({"app_name": "Production Hub", "active_profile": "Default Profile"})
        self.assertEqual("Production Hub - Audience Cam", config.integrations.video.audience_ndi_source_name)
        self.assertTrue(config.integrations.video.audience_auto_connect)
        self.assertFalse(config.integrations.video.ptz_auto_connect)
        self.assertLessEqual(config.integrations.video.preview_fps, 12)

    def test_broker_retains_only_latest_frame_and_copies_snapshots(self) -> None:
        broker = LatestFrameBroker()
        first = broker.publish(VideoSourceKey.AUDIENCE, image("#101010"), frame_rate=30)
        second = broker.publish(VideoSourceKey.AUDIENCE, image("#202020"), frame_rate=30)
        packet = broker.frame(VideoSourceKey.AUDIENCE)
        self.assertIsNotNone(packet)
        self.assertEqual(first.sequence + 1, second.sequence)
        self.assertEqual(second.sequence, packet.sequence)
        snapshot = broker.snapshot(VideoSourceKey.AUDIENCE)
        snapshot.message = "mutated test copy"
        self.assertEqual("Receiving video", broker.snapshot(VideoSourceKey.AUDIENCE).message)

    def test_ndi_source_matches_short_and_fully_qualified_names(self) -> None:
        source = AudienceNDISource(
            LatestFrameBroker(),
            NDISourceSettings("Production Hub - Audience Cam"),
        )
        self.assertTrue(source._matches_configured_source("Production Hub - Audience Cam"))
        self.assertTrue(
            source._matches_configured_source("OBS-MAC (Production Hub - Audience Cam)")
        )
        self.assertFalse(source._matches_configured_source("Production Hub - PTZ Cam"))

    def test_diagnostic_recording_can_be_replayed(self) -> None:
        with TemporaryDirectory() as directory:
            broker = LatestFrameBroker()
            recorder = DiagnosticRecorder(broker, Path(directory), frame_rate=8)
            recorder.initialize_qt()
            session = recorder.start((VideoSourceKey.AUDIENCE,))
            for index in range(16):
                broker.publish(VideoSourceKey.AUDIENCE, image(f"#{index + 1:02x}3050"), frame_rate=30)
                APP.processEvents()
                time.sleep(0.04)
            recorder.stop(wait=True)

            video_path = session / "audience.mp4"
            self.assertEqual("", recorder.last_error)
            self.assertTrue(video_path.is_file())
            self.assertTrue((session / "manifest.json").is_file())

            replay = ReplayVideoSource(broker)
            replay.initialize_qt()
            replay.start(video_path)
            deadline = time.monotonic() + 3
            while replay.running and time.monotonic() < deadline:
                APP.processEvents()
                time.sleep(0.02)
            replay.stop()
            packet = broker.frame(VideoSourceKey.REPLAY)
            self.assertIsNotNone(packet)
            self.assertEqual((320, 180), (packet.width, packet.height))
            self.assertEqual(VideoSourceState.STOPPED, broker.snapshot(VideoSourceKey.REPLAY).state)


if __name__ == "__main__":
    unittest.main()
