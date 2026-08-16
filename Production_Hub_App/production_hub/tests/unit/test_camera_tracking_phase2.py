from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from production_hub.api.server import create_app
from production_hub.app.bootstrap import build_context
from production_hub.core.config.models import (
    AppConfig,
    CameraSceneRegion,
    CameraTrackingConfig,
    SceneRegionPoint,
)
from production_hub.app.dev_launcher import (
    DEV_CHILD_ARGUMENT,
    should_use_development_app,
    without_dev_child_argument,
)
from production_hub.tracking.association import SubjectAssociator
from production_hub.tracking.models import (
    NormalizedRect,
    PersonCandidate,
    TrackingState,
)
from production_hub.tracking.service import (
    PersonTrackingService,
    _deduplicate_candidates,
    _region_detection_windows,
)
from production_hub.ui.pages.camera_control_page import CameraControlPage
from production_hub.video.frame_broker import LatestFrameBroker
from production_hub.video.models import VideoSourceKey


APP = QApplication.instance() or QApplication([])


def frame() -> QImage:
    image = QImage(640, 360, QImage.Format.Format_RGB32)
    image.fill(QColor("#172033"))
    return image


class _MovingDetector:
    backend_name = "Test detector"

    def __init__(self, _config: CameraTrackingConfig) -> None:
        self.calls = 0

    def detect(self, _image: QImage) -> list[PersonCandidate]:
        self.calls += 1
        x = 0.10 + min(self.calls - 1, 2) * 0.02
        return [PersonCandidate(NormalizedRect(x, 0.20, 0.20, 0.60), 0.91)]


class _SlowDetector:
    backend_name = "Slow test detector"

    def __init__(self, _config: CameraTrackingConfig) -> None:
        self.calls = 0

    def detect(self, _image: QImage) -> list[PersonCandidate]:
        self.calls += 1
        time.sleep(0.06)
        return [PersonCandidate(NormalizedRect(0.30, 0.20, 0.20, 0.60), 0.88)]


class CameraTrackingPhaseTwoTests(unittest.TestCase):
    def test_read_only_tracking_api_exposes_both_sources(self) -> None:
        with TemporaryDirectory() as directory:
            context = build_context(Path(directory))
            context.config.integrations.camera_tracking.scene_regions = [
                CameraSceneRegion(
                    id="stage",
                    name="Stage",
                    kind="stage",
                    points=[
                        SceneRegionPoint(0.1, 0.2),
                        SceneRegionPoint(0.9, 0.2),
                        SceneRegionPoint(0.8, 0.8),
                    ],
                )
            ]
            client = TestClient(create_app(context))
            response = client.get("/api/camera/tracking")
            self.assertEqual(200, response.status_code)
            self.assertEqual({"audience", "ptz"}, set(response.json()))
            self.assertEqual("disabled", response.json()["audience"]["state"])
            self.assertEqual(404, client.get("/api/camera/tracking/replay").status_code)
            regions = client.get("/api/camera/regions")
            self.assertEqual(200, regions.status_code)
            self.assertEqual("Stage", regions.json()["regions"][0]["name"])
            self.assertFalse(regions.json()["regions"][0]["suggested"])
            self.assertEqual(
                "calibration_reference",
                regions.json()["regions"][0]["coordinateSpace"],
            )
            calibration = client.get("/api/camera/calibration")
            self.assertEqual(200, calibration.status_code)
            self.assertFalse(calibration.json()["motionSafe"])
            context.video.shutdown()

    def test_popup_editor_adds_and_bulk_deletes_suggested_drawings(self) -> None:
        with TemporaryDirectory() as directory:
            context = build_context(Path(directory))
            context.config.integrations.video.audience_auto_connect = False
            context.config.integrations.video.ptz_auto_connect = False
            page = CameraControlPage(context)
            page.audience_preview.set_frame(frame(), 1)
            page._sync_region_editor_frame()
            self.assertTrue(page.region_editor_preview.has_image)

            page.add_suggested_scene_regions()
            regions = context.config.integrations.camera_tracking.scene_regions
            self.assertEqual(10, len(regions))
            self.assertTrue(all(region.suggested for region in regions))
            self.assertEqual(10, page.region_list.count())
            self.assertIn("10 suggested", page.region_summary.text())
            self.assertTrue(page.region_show_selected_only.isChecked())
            self.assertEqual(1, len(page.region_editor_preview._regions))
            page.region_show_selected_only.setChecked(False)
            self.assertEqual(10, len(page.region_editor_preview._regions))

            page.region_list.clearSelection()
            page.region_list.item(0).setSelected(True)
            page.region_list.item(1).setSelected(True)
            page.delete_scene_region()
            self.assertEqual(
                8,
                len(context.config.integrations.camera_tracking.scene_regions),
            )
            page.close()
            context.video.shutdown()

    def test_empty_room_region_can_be_saved_without_detected_subjects(self) -> None:
        with TemporaryDirectory() as directory:
            context = build_context(Path(directory))
            context.config.integrations.video.audience_auto_connect = False
            context.config.integrations.video.ptz_auto_connect = False
            page = CameraControlPage(context)
            page.audience_preview.set_frame(frame(), 1)
            page._region_draft_name = "Stage"
            page._region_draft_kind = "stage"
            page._region_draft_color = "#25d0c8"
            page.audience_preview.set_region_draft((), drawing=True)
            page.add_scene_region_point(0.1, 0.2)
            page.add_scene_region_point(0.9, 0.2)
            page.add_scene_region_point(0.8, 0.8)
            page.finish_scene_region()
            regions = context.config.integrations.camera_tracking.scene_regions
            self.assertEqual(1, len(regions))
            self.assertEqual("stage", regions[0].kind)
            original_id = regions[0].id

            page.region_list.setCurrentRow(0)
            page.redraw_scene_region()
            page.add_scene_region_point(0.2, 0.3)
            page.add_scene_region_point(0.8, 0.3)
            page.add_scene_region_point(0.7, 0.7)
            page.finish_scene_region()
            regions = context.config.integrations.camera_tracking.scene_regions
            self.assertEqual(1, len(regions))
            self.assertEqual(original_id, regions[0].id)
            self.assertAlmostEqual(0.2, regions[0].points[0].x)

            persisted = context.config_repository.load_app_config()
            persisted_regions = persisted.integrations.camera_tracking.scene_regions
            self.assertEqual(original_id, persisted_regions[0].id)
            self.assertAlmostEqual(0.2, persisted_regions[0].points[0].x)
            self.assertFalse(
                page.select_all_subject_buttons[VideoSourceKey.AUDIENCE].isEnabled()
            )
            page.close()
            context.video.shutdown()

    def test_source_gui_runs_use_stable_development_bundle_identity(self) -> None:
        self.assertTrue(should_use_development_app([]))
        self.assertTrue(should_use_development_app(["--no-api"]))
        self.assertFalse(should_use_development_app(["--api-only"]))
        self.assertFalse(should_use_development_app([DEV_CHILD_ARGUMENT]))
        self.assertEqual([], without_dev_child_argument([DEV_CHILD_ARGUMENT]))

    def test_old_configs_receive_safe_tracking_defaults(self) -> None:
        config = AppConfig.from_dict({"app_name": "Production Hub"})
        tracking = config.integrations.camera_tracking
        self.assertFalse(tracking.enabled)
        self.assertTrue(tracking.analyze_audience)
        self.assertTrue(tracking.analyze_ptz)
        self.assertLessEqual(tracking.analysis_fps, 4.0)

    def test_association_keeps_identity_and_selection_across_small_motion(self) -> None:
        associator = SubjectAssociator()
        first = associator.update(
            [PersonCandidate(NormalizedRect(0.10, 0.20, 0.20, 0.60), 0.9)]
        )
        self.assertEqual(1, len(first))
        track_id = first[0].track_id
        self.assertTrue(associator.toggle(track_id))

        second = associator.update(
            [PersonCandidate(NormalizedRect(0.13, 0.20, 0.20, 0.60), 0.92)]
        )
        self.assertEqual(track_id, second[0].track_id)
        self.assertTrue(second[0].selected)

    def test_tracking_service_reports_and_selects_subjects(self) -> None:
        broker = LatestFrameBroker()
        config = CameraTrackingConfig(
            enabled=True,
            analyze_audience=True,
            analyze_ptz=False,
            analysis_fps=12,
        )
        detector = _MovingDetector(config)
        service = PersonTrackingService(
            broker,
            config,
            detector_factory=lambda _config: detector,
        )
        service.start()
        try:
            first_packet = broker.publish(VideoSourceKey.AUDIENCE, frame(), frame_rate=30)
            first = self._wait_for_sequence(service, first_packet.sequence)
            self.assertEqual(TrackingState.RUNNING, first.state)
            self.assertEqual(1, len(first.subjects))
            track_id = first.subjects[0].track_id
            self.assertTrue(service.toggle_subject(VideoSourceKey.AUDIENCE, track_id))

            second_packet = broker.publish(VideoSourceKey.AUDIENCE, frame(), frame_rate=30)
            second = self._wait_for_sequence(service, second_packet.sequence)
            self.assertEqual(track_id, second.subjects[0].track_id)
            self.assertTrue(second.subjects[0].selected)
            self.assertEqual("Test detector", second.backend)
        finally:
            service.stop()

    def test_tracking_worker_drops_superseded_frames(self) -> None:
        broker = LatestFrameBroker()
        config = CameraTrackingConfig(
            enabled=True,
            analyze_audience=True,
            analyze_ptz=False,
            analysis_fps=12,
        )
        detector = _SlowDetector(config)
        service = PersonTrackingService(
            broker,
            config,
            detector_factory=lambda _config: detector,
        )
        service.start()
        try:
            latest_sequence = 0
            for _index in range(30):
                packet = broker.publish(VideoSourceKey.AUDIENCE, frame(), frame_rate=30)
                latest_sequence = packet.sequence
                time.sleep(0.003)
            snapshot = self._wait_for_sequence(service, latest_sequence)
            self.assertEqual(latest_sequence, snapshot.analyzed_sequence)
            self.assertLess(detector.calls, 10)
        finally:
            service.stop()

    def test_wide_stage_is_split_into_bounded_overlapping_detection_windows(self) -> None:
        region = CameraSceneRegion(
            id="wide-stage",
            name="Wide Stage",
            kind="stage",
            points=[
                SceneRegionPoint(0.10, 0.05),
                SceneRegionPoint(0.90, 0.05),
                SceneRegionPoint(0.90, 0.40),
                SceneRegionPoint(0.10, 0.40),
            ],
        )

        windows = _region_detection_windows((region,))

        self.assertEqual(3, len(windows))
        self.assertTrue(all(window.width <= 0.300001 for window in windows))
        self.assertLess(windows[0].x, windows[1].x)
        self.assertLess(windows[1].x, windows[2].x)
        self.assertGreaterEqual(windows[0].y, 0.0)

    def test_overlapping_crop_detections_are_deduplicated(self) -> None:
        candidates = [
            PersonCandidate(NormalizedRect(0.40, 0.10, 0.08, 0.25), 0.82),
            PersonCandidate(NormalizedRect(0.405, 0.105, 0.08, 0.25), 0.91),
        ]

        selected = _deduplicate_candidates(candidates)

        self.assertEqual(1, len(selected))
        self.assertEqual(0.91, selected[0].confidence)

    def _wait_for_sequence(
        self,
        service: PersonTrackingService,
        sequence: int,
    ):
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            snapshot = service.snapshot(VideoSourceKey.AUDIENCE)
            if snapshot.analyzed_sequence >= sequence:
                return snapshot
            time.sleep(0.01)
        self.fail(f"Tracking did not analyze frame sequence {sequence}")


if __name__ == "__main__":
    unittest.main()
