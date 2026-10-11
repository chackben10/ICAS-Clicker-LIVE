from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from production_hub.app.bootstrap import build_context
from production_hub.core.config.models import AppConfig
from production_hub.core.config.models import VideoConfig
from production_hub.video.frame_broker import LatestFrameBroker
from production_hub.video.local_camera_source import LocalCameraVideoSource
from production_hub.video.models import VideoSourceKey, VideoSourceState
from production_hub.video.ndi_source import AudienceNDISource, NDISourceSettings, NDIVideoSource
from production_hub.video.recording import DiagnosticRecorder, ReplayVideoSource
from production_hub.video.service import VideoService
from production_hub.ui.pages.camera_control_page import CameraControlPage


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
        self.assertTrue(config.integrations.video.ptz_auto_connect)
        self.assertEqual("ndi", config.integrations.video.audience_source_type)
        self.assertEqual("local", config.integrations.video.ptz_source_type)
        self.assertLessEqual(config.integrations.video.preview_fps, 12)

    def test_both_video_slots_can_switch_between_ndi_and_local_sources(self) -> None:
        with TemporaryDirectory() as directory:
            config = VideoConfig(audience_auto_connect=False, ptz_auto_connect=False)
            service = VideoService(config, Path(directory))
            service.initialize_qt()
            self.assertIsInstance(service.source_pipeline(VideoSourceKey.AUDIENCE), NDIVideoSource)
            self.assertIsInstance(service.source_pipeline(VideoSourceKey.PTZ), LocalCameraVideoSource)

            updated = VideoConfig.from_dict(config.to_dict())
            updated.audience_source_type = "local"
            updated.audience_device_id = "audience-device"
            updated.ptz_source_type = "ndi"
            updated.ptz_ndi_source_name = "Production Hub - PTZ Cam"
            service.reconfigure(updated)
            self.assertIsInstance(service.source_pipeline(VideoSourceKey.AUDIENCE), LocalCameraVideoSource)
            self.assertIsInstance(service.source_pipeline(VideoSourceKey.PTZ), NDIVideoSource)
            service.shutdown()

    def test_denied_camera_permission_is_reported_instead_of_hanging_on_starting(self) -> None:
        broker = LatestFrameBroker()
        source = LocalCameraVideoSource(broker)
        original = source._camera_permission_status
        source._camera_permission_status = lambda: Qt.PermissionStatus.Denied
        try:
            source.start("camera-device")
            snapshot = broker.snapshot(VideoSourceKey.PTZ)
            self.assertEqual(VideoSourceState.PERMISSION_DENIED, snapshot.state)
            self.assertIn("Privacy & Security", snapshot.message)
        finally:
            source._camera_permission_status = original

    def test_local_camera_capture_suspends_when_no_feature_needs_frames(self) -> None:
        class Camera:
            def __init__(self) -> None:
                self.active = True
                self.starts = 0
                self.stops = 0

            def isActive(self) -> bool:
                return self.active

            def start(self) -> None:
                self.active = True
                self.starts += 1

            def stop(self) -> None:
                self.active = False
                self.stops += 1

        broker = LatestFrameBroker()
        source = LocalCameraVideoSource(broker)
        camera = Camera()
        source.camera = camera
        source._output_active = True

        source.set_output_active(False)
        self.assertEqual(1, camera.stops)
        self.assertFalse(camera.active)
        self.assertEqual(VideoSourceState.STOPPED, broker.snapshot(VideoSourceKey.PTZ).state)

        source.set_output_active(True)
        self.assertEqual(1, camera.starts)
        self.assertTrue(camera.active)
        source.camera = None

    def test_preview_owners_activate_only_the_sources_they_consume(self) -> None:
        with TemporaryDirectory() as directory:
            config = VideoConfig(audience_auto_connect=False, ptz_auto_connect=False)
            service = VideoService(config, Path(directory))
            audience = Mock()
            ptz = Mock()
            service._sources = {
                VideoSourceKey.AUDIENCE: audience,
                VideoSourceKey.PTZ: ptz,
            }

            service.set_preview_activity(
                True,
                owner="click_to_frame",
                sources={VideoSourceKey.AUDIENCE},
            )
            audience.set_output_active.assert_called_with(True)
            ptz.set_output_active.assert_called_with(False)

            service.set_preview_activity(
                True,
                owner="camera_page",
                sources={VideoSourceKey.AUDIENCE, VideoSourceKey.PTZ},
            )
            ptz.set_output_active.assert_called_with(True)

            service.set_preview_activity(False, owner="camera_page")
            audience.set_output_active.assert_called_with(True)
            ptz.set_output_active.assert_called_with(False)

            service.set_preview_activity(False, owner="click_to_frame")
            audience.set_output_active.assert_called_with(False)
            ptz.set_output_active.assert_called_with(False)

    def test_camera_control_preset_recall_uses_panasonic_service(self) -> None:
        with TemporaryDirectory() as directory:
            context = build_context(Path(directory))
            context.config.integrations.video.audience_auto_connect = False
            context.config.integrations.video.ptz_auto_connect = False
            context.panasonic.recall_preset = AsyncMock(return_value=True)
            page = CameraControlPage(context)
            page.preset_list.setCurrentRow(7)
            page.recall_selected_preset()
            deadline = time.monotonic() + 2
            while context.panasonic.recall_preset.await_count == 0 and time.monotonic() < deadline:
                APP.processEvents()
                time.sleep(0.01)
            context.panasonic.recall_preset.assert_awaited_once_with(7)
            deadline = time.monotonic() + 2
            while "recalled" not in page.status.text().casefold() and time.monotonic() < deadline:
                APP.processEvents()
                time.sleep(0.01)
            self.assertIn("Preset 07 recalled", page.status.text())
            page.close()
            context.video.shutdown()

    def test_camera_page_construction_does_not_request_camera_access(self) -> None:
        with TemporaryDirectory() as directory:
            context = build_context(Path(directory))
            context.config.integrations.video.audience_auto_connect = False
            context.config.integrations.video.ptz_auto_connect = False
            with patch.object(context.video, "initialize_qt") as initialize:
                page = CameraControlPage(context)
                initialize.assert_not_called()
                page._ensure_video_initialized()
                initialize.assert_called_once_with()
            page.close()
            context.video.shutdown()

    def test_recording_controls_enable_once_video_services_start(self) -> None:
        # The diagnostics group is built before the lazy video start, when the
        # recorder does not exist yet. The buttons must not stay disabled.
        with TemporaryDirectory() as directory:
            context = build_context(Path(directory))
            context.config.integrations.video.audience_auto_connect = False
            context.config.integrations.video.ptz_auto_connect = False
            page = CameraControlPage(context)
            self.assertFalse(context.video.qt_initialized)
            self.assertFalse(page.record_button.isEnabled())
            self.assertTrue(page.recording_unavailable_label.isHidden())

            page._ensure_video_initialized()

            self.assertTrue(context.video.recording_available)
            self.assertTrue(page.record_button.isEnabled())
            self.assertTrue(page.replay_button.isEnabled())
            self.assertTrue(page.recording_unavailable_label.isHidden())
            page.close()
            context.video.shutdown()

    def test_diagnostic_recorder_is_shown_only_in_production_hub_dev(self) -> None:
        for dev_build in (False, True):
            with self.subTest(dev_build=dev_build), TemporaryDirectory() as directory:
                context = build_context(Path(directory))
                context.config.integrations.video.audience_auto_connect = False
                context.config.integrations.video.ptz_auto_connect = False
                context.ptz_automation.tracking_refinements_enabled = dev_build
                page = CameraControlPage(context)
                placed = page._advanced_diagnostics_group.parent() is not None
                self.assertEqual(placed, dev_build)
                page.close()
                context.video.shutdown()

    def test_recording_unavailable_notice_appears_only_after_a_failed_start(self) -> None:
        with TemporaryDirectory() as directory:
            context = build_context(Path(directory))
            context.config.integrations.video.audience_auto_connect = False
            context.config.integrations.video.ptz_auto_connect = False
            page = CameraControlPage(context)
            with patch.object(context.video.recorder, "initialize_qt"):
                page._ensure_video_initialized()
            self.assertFalse(context.video.recording_available)
            self.assertFalse(page.record_button.isEnabled())
            self.assertFalse(page.recording_unavailable_label.isHidden())
            page.close()
            context.video.shutdown()

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

    def test_ndi_receiver_does_not_drain_frames_while_output_is_inactive(self) -> None:
        class Library:
            def __init__(self) -> None:
                self.capture_calls = 0
                self.destroy_calls = 0

            def ph_ndi_receiver_capture_video(self, *_args) -> int:
                self.capture_calls += 1
                return 0

            def ph_ndi_receiver_destroy(self, _receiver) -> None:
                self.destroy_calls += 1

        class Runtime:
            def __init__(self) -> None:
                self.library = Library()

        source = AudienceNDISource(
            LatestFrameBroker(),
            NDISourceSettings("Production Hub - Audience Cam"),
        )
        runtime = Runtime()

        self.assertIsNone(source._capture_until_reconnect(runtime, object()))
        self.assertEqual(0, runtime.library.capture_calls)
        self.assertEqual(1, runtime.library.destroy_calls)

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


def test_a_shared_capture_card_keeps_its_own_format() -> None:
    """ProPresenter may hold the same card; do not reconfigure it underneath.

    A UVC device streams one format for every client. Forcing one here makes
    macOS convert frames for whichever client lost, and the reconfiguration can
    disturb the other application's feed. Production Hub already drops frames
    to preview_fps, so it consumes the device's own format and uses fewer of
    those frames.
    """

    from production_hub.core.config.models import VideoConfig

    assert VideoConfig().match_device_capture_format is True
