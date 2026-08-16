from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import cv2
import numpy as np
from PySide6.QtWidgets import QApplication, QMessageBox

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from production_hub.calibration.relocalization import (
    AudienceRelocalizer,
    RelocalizationState,
    _is_motion_safe_lock,
)
from production_hub.calibration.review import (
    CalibrationReviewData,
    CalibrationReviewMarker,
    load_active_calibration_review,
    load_latest_calibration_review,
)
from production_hub.calibration.store import CalibrationRegistry
from production_hub.app.bootstrap import (
    build_context,
    ensure_camera_calibration_defaults,
    ensure_latest_automatic_calibration,
)
from production_hub.core.config.models import (
    AppConfig,
    CameraSceneRegion,
    SceneRegionPoint,
)
from production_hub.tracking.scene_regions import transform_scene_regions
from production_hub.ui.pages.camera_control_page import CameraControlPage


APP = QApplication.instance() or QApplication([])


class CameraCalibrationPhaseThreeABCTests(unittest.TestCase):
    def test_startup_activates_latest_successful_automatic_calibration(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            map_path = self._write_map(
                root,
                "automatic",
                "2026-08-11T09:00:00+00:00",
            )
            payload = json.loads(map_path.read_text(encoding="utf-8"))
            payload["schema_version"] = 2
            payload["purpose"] = CalibrationRegistry.AUTOMATIC_MAP_PURPOSE
            payload["poses"] = [
                {
                    **payload["poses"][0],
                    "index": index,
                    "name": f"pose-{index}",
                }
                for index in range(1, 5)
            ]
            map_path.write_text(json.dumps(payload), encoding="utf-8")

            activated = ensure_latest_automatic_calibration(root)

            self.assertEqual(map_path.resolve(), activated)
            registry = CalibrationRegistry(root)
            self.assertEqual(map_path.resolve(), registry.active_map_path())
            self.assertEqual("approved", registry.curation(map_path).approval_status)

    def test_high_precision_stage_cluster_is_motion_safe(self) -> None:
        self.assertTrue(
            _is_motion_safe_lock(
                inliers=35,
                inlier_ratio=35 / 37,
                median_error_pixels=0.66,
                reference_coverage=0.0287,
                plausible=True,
            )
        )
        self.assertFalse(
            _is_motion_safe_lock(
                inliers=20,
                inlier_ratio=0.55,
                median_error_pixels=3.0,
                reference_coverage=0.0287,
                plausible=True,
            )
        )
        self.assertTrue(
            _is_motion_safe_lock(
                inliers=25,
                inlier_ratio=0.86,
                median_error_pixels=1.21,
                reference_coverage=0.018,
                plausible=True,
            )
        )
        self.assertFalse(
            _is_motion_safe_lock(
                inliers=23,
                inlier_ratio=0.90,
                median_error_pixels=1.10,
                reference_coverage=0.018,
                plausible=True,
            )
        )

    def test_review_ui_excludes_restores_and_approves_without_rewriting_map(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            map_path = self._write_map(root, "review", "2026-08-09T09:00:00+00:00")
            original = map_path.read_bytes()
            context = build_context(root)
            context.config.integrations.video.audience_auto_connect = False
            context.config.integrations.video.ptz_auto_connect = False
            page = CameraControlPage(context)
            page.calibration_marker_list.setCurrentRow(0)
            selected = page.selected_calibration_marker_id()
            page.exclude_selected_calibration_marker()
            self.assertEqual(original, map_path.read_bytes())
            self.assertEqual(29, page._calibration_review_data.marker_count)
            self.assertEqual("pending_review", page._calibration_review_data.approval_status)

            with patch(
                "production_hub.ui.pages.camera_control_page.QInputDialog.getItem",
                return_value=(f"M{selected:03d}", True),
            ):
                page.restore_excluded_calibration_marker()
            self.assertEqual(30, page._calibration_review_data.marker_count)
            with patch(
                "production_hub.ui.pages.camera_control_page.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                page.approve_and_activate_calibration()
            self.assertEqual("approved", page._calibration_review_data.approval_status)
            self.assertEqual(original, map_path.read_bytes())
            page.close()
            context.video.shutdown()

    def test_marker_curation_requires_reapproval_and_supports_rollback(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._write_map(root, "first", "2026-08-09T08:00:00+00:00")
            second = self._write_map(root, "second", "2026-08-09T09:00:00+00:00")
            registry = CalibrationRegistry(root)

            registry.approve_and_activate(first)
            self.assertEqual(first.resolve(), registry.active_map_path())
            registry.exclude_marker(first, 1)
            self.assertEqual("pending_review", registry.curation(first).approval_status)
            self.assertIsNone(load_active_calibration_review(root))

            registry.approve_and_activate(first)
            review = load_active_calibration_review(root)
            self.assertIsNotNone(review)
            assert review is not None
            self.assertEqual(29, review.marker_count)
            self.assertEqual([1], [item.marker_id for item in review.excluded_markers])

            registry.approve_and_activate(second)
            self.assertEqual(second.resolve(), registry.active_map_path())
            self.assertEqual(first.resolve(), registry.rollback())
            self.assertEqual(first.resolve(), registry.active_map_path())

    def test_relocalizer_tracks_curated_reference_under_camera_drift(self) -> None:
        reference, markers = self._feature_scene()
        matrix = np.asarray(
            [[0.995, -0.018, 24.0], [0.014, 1.002, -13.0], [0.00001, -0.00002, 1.0]],
            dtype=np.float64,
        )
        live = cv2.warpPerspective(reference, matrix, (reference.shape[1], reference.shape[0]))
        review = CalibrationReviewData(
            map_path=Path("/tmp/test-map.json"),
            created_at="2026-08-09T09:00:00+00:00",
            status="accepted",
            audience_image_path=Path("/tmp/reference.jpg"),
            reference_calibration_path=Path("/tmp/reference.json"),
            audience_size=(reference.shape[1], reference.shape[0]),
            audience_markers=markers,
            excluded_markers=(),
            poses=(),
            approval_status="approved",
        )
        result = AudienceRelocalizer(reference, review, maximum_width=960).estimate(live)
        self.assertEqual(RelocalizationState.LOCKED, result.state, result.message)
        self.assertGreaterEqual(result.inliers, 18)
        self.assertLess(result.median_error_pixels, 2.0)
        actual = np.asarray(result.reference_to_live)
        source = np.asarray([reference.shape[1] * 0.5, reference.shape[0] * 0.5, 1.0])
        expected_point = matrix @ source
        actual_point = actual @ source
        expected_point /= expected_point[2]
        actual_point /= actual_point[2]
        self.assertLess(float(np.linalg.norm(expected_point[:2] - actual_point[:2])), 3.0)

    def test_scene_planes_project_from_reference_into_live_coordinates(self) -> None:
        region = CameraSceneRegion(
            id="stage",
            name="Stage",
            kind="stage",
            coordinate_space="calibration_reference",
            calibration_reference="map-one",
            points=[
                SceneRegionPoint(0.1, 0.2),
                SceneRegionPoint(0.5, 0.2),
                SceneRegionPoint(0.5, 0.6),
            ],
        )
        matrix = ((1.0, 0.0, 100.0), (0.0, 1.0, 50.0), (0.0, 0.0, 1.0))
        transformed = transform_scene_regions([region], matrix, (1000, 500), (1000, 500))
        self.assertEqual(1, len(transformed))
        self.assertEqual("live_audience", transformed[0].coordinate_space)
        self.assertAlmostEqual(0.2, transformed[0].points[0].x)
        self.assertAlmostEqual(0.3, transformed[0].points[0].y)
        self.assertEqual("map-one", transformed[0].calibration_reference)

    def test_phase_two_drawings_migrate_to_current_calibration_reference(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            map_path = self._write_map(root, "active", "2026-08-09T09:00:00+00:00")
            CalibrationRegistry(root).approve_and_activate(map_path)
            config = AppConfig()
            config.integrations.camera_tracking.scene_regions = [
                CameraSceneRegion(
                    id="suggested-podium-v1",
                    name="Podium",
                    kind="custom",
                    points=[
                        SceneRegionPoint(0.1, 0.1),
                        SceneRegionPoint(0.2, 0.1),
                        SceneRegionPoint(0.2, 0.3),
                    ],
                )
            ]
            self.assertTrue(ensure_camera_calibration_defaults(config, root))
            region = config.integrations.camera_tracking.scene_regions[0]
            self.assertEqual("podium", region.kind)
            self.assertEqual("calibration_reference", region.coordinate_space)
            self.assertEqual("2026-08-09T09:00:00+00:00", region.calibration_reference)

    @staticmethod
    def _feature_scene() -> tuple[np.ndarray, tuple[CalibrationReviewMarker, ...]]:
        image = np.full((540, 960, 3), 32, dtype=np.uint8)
        markers: list[CalibrationReviewMarker] = []
        marker_id = 1
        for row, y in enumerate(range(70, 500, 80)):
            for column, x in enumerate(range(80, 900, 130)):
                color = (
                    80 + (row * 31) % 150,
                    80 + (column * 43) % 150,
                    100 + ((row + column) * 27) % 130,
                )
                cv2.rectangle(image, (x - 22, y - 18), (x + 24, y + 20), color, 2)
                cv2.line(image, (x - 28, y), (x + 30, y), (245, 245, 245), 2)
                cv2.line(image, (x, y - 25), (x, y + 27), (220, 220, 220), 2)
                cv2.putText(
                    image,
                    f"{marker_id:02d}",
                    (x - 13, y + 7),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.45,
                    color,
                    1,
                    cv2.LINE_AA,
                )
                markers.append(
                    CalibrationReviewMarker(
                        marker_id,
                        x / image.shape[1],
                        y / image.shape[0],
                        0.0,
                        0.0,
                        0.0,
                        repeatability=3,
                        structure_score=1.0,
                        stability="temporal_repeat",
                    )
                )
                marker_id += 1
        return image, tuple(markers)

    @staticmethod
    def _write_map(root: Path, name: str, created_at: str) -> Path:
        reference_directory = root / "calibration" / "reference"
        sweep_directory = root / "calibration-sweeps" / name
        reference_directory.mkdir(parents=True, exist_ok=True)
        sweep_directory.mkdir(parents=True, exist_ok=True)
        reference_image = np.full((500, 1000, 3), 90, dtype=np.uint8)
        cv2.imwrite(str(reference_directory / "audience.jpg"), reference_image)
        cv2.imwrite(str(reference_directory / "ptz.jpg"), reference_image)
        reference_path = reference_directory / "calibration.json"
        reference_path.write_text(
            json.dumps(
                {
                    "alignment": {
                        "status": "accepted",
                        "audience_size": [1000, 500],
                        "ptz_size": [1000, 500],
                        "inliers": 30,
                        "median_error_pixels": 0.5,
                        "audience_to_ptz": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                        "ptz_to_audience": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                        "correspondences": [],
                    },
                    "artifacts": {"audience_image": "audience.jpg", "ptz_image": "ptz.jpg"},
                }
            ),
            encoding="utf-8",
        )
        markers = [
            {
                "marker_id": index,
                "audience_x": 30 + index * 30,
                "audience_y": 40 + (index % 10) * 35,
                "ptz_x": 30 + index * 30,
                "ptz_y": 40 + (index % 10) * 35,
                "error_pixels": 0.5,
                "repeatability": 2,
                "structure_score": 0.8,
            }
            for index in range(1, 31)
        ]
        path = sweep_directory / "full_sync.json"
        path.write_text(
            json.dumps(
                {
                    "created_at": created_at,
                    "status": "accepted",
                    "approval_status": "pending_review",
                    "reference_calibration": str(reference_path),
                    "structural_markers": markers,
                    "poses": [
                        {
                            "index": 1,
                            "name": "reference",
                            "status": "accepted",
                            "ptz_size": [1000, 500],
                            "structural_markers": markers,
                            "motor_position": {"pan": 1, "tilt": 2, "zoom": 3},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path


if __name__ == "__main__":
    unittest.main()
