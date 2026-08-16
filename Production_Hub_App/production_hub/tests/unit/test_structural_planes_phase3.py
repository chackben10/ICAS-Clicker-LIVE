from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import cv2
import numpy as np
from PySide6.QtGui import QColor, QImage

from production_hub.calibration.structural_planes import (
    StructuralPlaneInput,
    StructuralPlaneSettings,
    extract_structural_planes,
)
from production_hub.core.config.models import CameraTrackingConfig, VideoConfig
from production_hub.tracking.models import NormalizedRect, PersonCandidate, TrackingState
from production_hub.tracking.scene_regions import structural_plane_regions
from production_hub.tracking.service import PersonTrackingService
from production_hub.video.frame_broker import LatestFrameBroker
from production_hub.video.models import VideoSourceKey
from production_hub.video.service import VideoService


class _Detector:
    backend_name = "Activity-gate detector"

    def __init__(self) -> None:
        self.calls = 0

    def detect(self, _image: QImage) -> list[PersonCandidate]:
        self.calls += 1
        return [PersonCandidate(NormalizedRect(0.2, 0.2, 0.2, 0.5), 0.9)]


class _OutputProbe:
    def __init__(self) -> None:
        self.active = False

    def set_output_active(self, active: bool) -> None:
        self.active = bool(active)


class StructuralPlanePhaseThreeTests(unittest.TestCase):
    def test_repeated_cross_camera_models_become_reviewable_planes(self) -> None:
        audience = self._two_plane_scene()
        inputs = [
            StructuralPlaneInput(
                pose_index=index,
                pose_name=f"pose-{index}",
                audience_bgr=audience,
                ptz_bgr=self._two_plane_ptz(audience, offset=index * 3),
                audience_to_reference=np.eye(3),
            )
            for index in (1, 2, 3)
        ]
        result = extract_structural_planes(
            audience,
            inputs,
            StructuralPlaneSettings(
                maximum_width=640,
                maximum_features=4000,
                ratio_threshold=0.82,
                maximum_models_per_pose=5,
                minimum_model_inliers=10,
                minimum_component_points=6,
                component_radius_fraction=0.11,
                minimum_area_fraction=0.008,
                minimum_pose_confirmations=2,
            ),
        )
        self.assertGreaterEqual(len(result.planes), 2)
        self.assertTrue(all(plane.observation_count >= 2 for plane in result.planes))
        self.assertTrue(all(len(plane.polygon) >= 3 for plane in result.planes))

    def test_plane_artifact_preserves_cross_camera_evidence(self) -> None:
        regions = structural_plane_regions(
            {
                "created_at": "2026-08-09T18:00:00+00:00",
                "calibration_reference": "map-1",
                "planes": [
                    {
                        "id": "generated-structural-plane-one",
                        "name": "Structural Plane 01",
                        "polygon": [[0.1, 0.2], [0.4, 0.2], [0.35, 0.5]],
                        "color": "#25d0c8",
                        "supporting_poses": ["wide", "left", "right"],
                        "support_points": 78,
                        "confidence": 0.91,
                    }
                ],
            }
        )
        self.assertEqual(1, len(regions))
        region = regions[0]
        self.assertEqual("cross_camera_structural_plane", region.generation_method)
        self.assertEqual("map-1", region.calibration_reference)
        self.assertEqual(3, len(region.supporting_poses))
        self.assertEqual(78, region.support_points)
        self.assertAlmostEqual(0.91, region.confidence)

    def test_person_analysis_is_dormant_until_runtime_activity_is_granted(self) -> None:
        broker = LatestFrameBroker()
        config = CameraTrackingConfig(
            enabled=True,
            analyze_audience=True,
            analyze_ptz=False,
            analysis_fps=12,
        )
        detector = _Detector()
        service = PersonTrackingService(
            broker,
            config,
            detector_factory=lambda _config: detector,
            initially_active=False,
        )
        service.start()
        try:
            first = broker.publish(VideoSourceKey.AUDIENCE, self._frame(), frame_rate=30)
            time.sleep(0.15)
            self.assertEqual(0, detector.calls)
            self.assertEqual(
                TrackingState.IDLE,
                service.snapshot(VideoSourceKey.AUDIENCE).state,
            )
            service.set_active(True)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                snapshot = service.snapshot(VideoSourceKey.AUDIENCE)
                if snapshot.analyzed_sequence >= first.sequence:
                    break
                time.sleep(0.01)
            self.assertGreaterEqual(detector.calls, 1)
            self.assertEqual(TrackingState.RUNNING, snapshot.state)
        finally:
            service.stop()

    def test_video_outputs_are_not_held_active_by_enabled_but_idle_vision(self) -> None:
        with TemporaryDirectory() as directory:
            tracking = CameraTrackingConfig(enabled=True, relocalization_enabled=True)
            service = VideoService(
                VideoConfig(enabled=False),
                Path(directory),
                tracking_config=tracking,
            )
            audience = _OutputProbe()
            ptz = _OutputProbe()
            service._sources = {
                VideoSourceKey.AUDIENCE: audience,
                VideoSourceKey.PTZ: ptz,
            }
            service._update_output_activity()
            self.assertFalse(audience.active)
            self.assertFalse(ptz.active)
            service.set_tracking_activity(True)
            self.assertTrue(audience.active)
            self.assertTrue(ptz.active)
            service.set_tracking_activity(False)
            self.assertFalse(audience.active)
            self.assertFalse(ptz.active)

    @staticmethod
    def _frame() -> QImage:
        image = QImage(640, 360, QImage.Format.Format_RGB32)
        image.fill(QColor("#172033"))
        return image

    @staticmethod
    def _two_plane_scene() -> np.ndarray:
        rng = np.random.default_rng(2901)
        image = np.full((360, 640, 3), 18, dtype=np.uint8)
        for left, right, color in ((40, 290, (50, 110, 170)), (350, 610, (140, 85, 40))):
            patch = rng.integers(0, 150, size=(240, right - left, 3), dtype=np.uint8)
            patch = cv2.add(patch, np.asarray(color, dtype=np.uint8))
            image[60:300, left:right] = patch
            for y in range(75, 300, 35):
                cv2.line(image, (left + 8, y), (right - 8, y + 8), (245, 245, 245), 2)
            for x in range(left + 18, right, 42):
                cv2.circle(image, (x, 180), 8, (8, 8, 8), 2)
        return image

    @staticmethod
    def _two_plane_ptz(audience: np.ndarray, *, offset: int) -> np.ndarray:
        result = np.full_like(audience, 10)
        transforms = (
            np.asarray(
                [[0.94, 0.035, 22 + offset], [-0.02, 0.96, 10], [0.00008, 0.00003, 1]],
                dtype=np.float64,
            ),
            np.asarray(
                [[1.02, -0.025, -18 - offset], [0.018, 0.92, 18], [-0.00006, 0.00004, 1]],
                dtype=np.float64,
            ),
        )
        for (left, right), matrix in zip(((40, 290), (350, 610)), transforms):
            mask = np.zeros(audience.shape[:2], dtype=np.uint8)
            mask[60:300, left:right] = 255
            warped_image = cv2.warpPerspective(audience, matrix, (640, 360))
            warped_mask = cv2.warpPerspective(mask, matrix, (640, 360))
            result[warped_mask > 0] = warped_image[warped_mask > 0]
        return result


if __name__ == "__main__":
    unittest.main()
