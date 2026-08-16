from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import cv2
import numpy as np
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from production_hub.app.bootstrap import build_context
from production_hub.calibration.light_level import assess_camera_light
from production_hub.calibration.mosaic import (
    ensure_calibration_mosaic,
    load_calibration_mosaic,
)
from production_hub.calibration.review import load_latest_calibration_review
from production_hub.ui.pages.camera_control_page import CameraControlPage, VideoPreview
from production_hub.ui.main_window import (
    TRACKING_INDICATOR_TIMEOUT_MS,
    MainWindow,
    TrackingStatusIndicator,
)
from production_hub.video.models import VideoSourceKey
from scripts.calibrate_ptz_to_audience import main as calibration_workflow_main


APP = QApplication.instance() or QApplication([])


class CameraCalibrationReviewTests(unittest.TestCase):
    def test_tracking_indicator_dismisses_five_seconds_after_state_change(self) -> None:
        indicator = TrackingStatusIndicator()
        APP.processEvents()
        self.assertTrue(indicator.isVisible())
        self.assertTrue(indicator._dismiss_timer.isSingleShot())
        self.assertEqual(
            TRACKING_INDICATOR_TIMEOUT_MS,
            indicator._dismiss_timer.interval(),
        )

        indicator._dismiss_timer.timeout.emit()
        APP.processEvents()
        self.assertFalse(indicator.isVisible())

        # Polling the unchanged state must not make a dismissed indicator reappear.
        indicator.set_tracking(False)
        APP.processEvents()
        self.assertFalse(indicator.isVisible())

        indicator.set_tracking(True)
        APP.processEvents()
        self.assertTrue(indicator.isVisible())
        self.assertTrue(indicator._dismiss_timer.isActive())
        self.assertEqual("● TRACKING ON", indicator.label.text())
        indicator.close()

    def test_review_loads_reference_markers_and_projects_each_ptz_pose(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_review_fixture(root)
            review = load_latest_calibration_review(root)
            self.assertIsNotNone(review)
            assert review is not None
            self.assertEqual(2, review.marker_count)
            self.assertEqual(2, len(review.poses))
            reference_marker = review.poses[0].markers[0]
            moved_marker = review.poses[1].markers[0]
            self.assertAlmostEqual(0.10, reference_marker.audience_x)
            self.assertAlmostEqual(0.20, reference_marker.ptz_x)
            self.assertAlmostEqual(0.25, moved_marker.ptz_x)

    def test_popup_reviews_saved_markers_without_live_camera_access(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_review_fixture(root)
            context = build_context(root)
            context.config.integrations.video.audience_auto_connect = False
            context.config.integrations.video.ptz_auto_connect = False
            page = CameraControlPage(context)
            page.open_camera_calibration_dialog()
            APP.processEvents()
            self.assertEqual(2, page.calibration_pose_selector.count())
            self.assertEqual(2, page.calibration_marker_list.count())
            self.assertTrue(page.calibration_audience_preview.has_image)
            self.assertTrue(page.calibration_ptz_preview.has_image)
            self.assertEqual(
                2,
                len(page.calibration_audience_preview._calibration_markers),
            )
            page.calibration_pose_selector.setCurrentIndex(1)
            APP.processEvents()
            self.assertIn("motor", page.calibration_review_summary.text())
            page.close()
            context.video.shutdown()

    def test_structural_atlas_uses_global_audience_markers_and_per_pose_points(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_review_fixture(root)
            map_path = root / "calibration-sweeps" / "fixture" / "full_sync.json"
            payload = json.loads(map_path.read_text(encoding="utf-8"))
            atlas = [
                {
                    "marker_id": 1,
                    "audience_x": 100,
                    "audience_y": 100,
                    "ptz_x": 200,
                    "ptz_y": 100,
                    "error_pixels": 0.4,
                    "repeatability": 3,
                    "stability": "temporal_repeat",
                },
                {
                    "marker_id": 2,
                    "audience_x": 800,
                    "audience_y": 400,
                    "ptz_x": 500,
                    "ptz_y": 200,
                    "error_pixels": 0.6,
                    "repeatability": 2,
                    "stability": "temporal_repeat",
                },
            ]
            payload["structural_markers"] = atlas
            payload["poses"][0]["ptz_size"] = [1000, 500]
            payload["poses"][0]["structural_markers"] = [atlas[0]]
            payload["poses"][1]["ptz_size"] = [1000, 500]
            payload["poses"][1]["structural_markers"] = [atlas[1]]
            map_path.write_text(json.dumps(payload), encoding="utf-8")

            review = load_latest_calibration_review(root)
            self.assertIsNotNone(review)
            assert review is not None
            self.assertEqual(2, review.marker_count)
            self.assertEqual([1], [item.marker_id for item in review.poses[0].markers])
            self.assertEqual([2], [item.marker_id for item in review.poses[1].markers])
            self.assertAlmostEqual(0.8, review.audience_markers[1].audience_x)

    def test_light_gate_rejects_dark_frames_and_accepts_detailed_frames(self) -> None:
        dark = QImage(320, 180, QImage.Format.Format_RGB32)
        dark.fill(QColor("#030303"))
        self.assertFalse(assess_camera_light(dark).acceptable)

        detailed = QImage(320, 180, QImage.Format.Format_RGB32)
        for y in range(detailed.height()):
            for x in range(detailed.width()):
                value = 35 + ((x // 16 + y // 16) % 2) * 120
                detailed.setPixelColor(x, y, QColor(value, value, value))
        assessment = assess_camera_light(detailed)
        self.assertTrue(assessment.acceptable, assessment.message)

    def test_click_to_frame_mosaic_extends_and_maps_past_audience_edges(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_review_fixture(root)
            map_path = root / "calibration-sweeps" / "fixture" / "full_sync.json"
            payload = json.loads(map_path.read_text(encoding="utf-8"))
            payload["poses"][0].update(
                {
                    "ptz_size": [1000, 500],
                    "audience_size": [1000, 500],
                    "ptz_to_audience": [[1, 0, -200], [0, 1, 0], [0, 0, 1]],
                }
            )
            payload["poses"][1].update(
                {
                    "ptz_size": [1000, 500],
                    "audience_size": [1000, 500],
                    "ptz_to_audience": [[1, 0, 200], [0, 1, 0], [0, 0, 1]],
                }
            )
            map_path.write_text(json.dumps(payload), encoding="utf-8")
            review = load_latest_calibration_review(root)
            self.assertIsNotNone(review)
            assert review is not None

            mosaic = ensure_calibration_mosaic(review, maximum_width=900)

            self.assertLess(mosaic.left, 0.0)
            self.assertGreater(mosaic.right, 1.0)
            self.assertTrue(mosaic.image_path.is_file())
            self.assertEqual(mosaic, load_calibration_mosaic(review))
            audience_left, _top, audience_width, _height = (
                mosaic.audience_canvas_rect
            )
            reference_left = mosaic.canvas_point_to_reference(audience_left, 0.5)[0]
            reference_right = mosaic.canvas_point_to_reference(
                audience_left + audience_width,
                0.5,
            )[0]
            # Click coordinates now live in the canonical reference-PTZ image,
            # so the fixture's +100px Audience-to-PTZ offset is preserved.
            self.assertAlmostEqual(0.10, reference_left)
            self.assertAlmostEqual(1.10, reference_right)
            matrix = mosaic.live_to_canvas_homography(
                ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
                source_size=review.audience_size,
                live_size=review.audience_size,
                reference_size=review.audience_size,
            )
            transform = CameraControlPage._qtransform_from_homography(matrix)
            mapped_top_left = transform.map(0.0, 0.0)
            mapped_bottom_right = transform.map(
                float(review.audience_size[0]),
                float(review.audience_size[1]),
            )
            self.assertAlmostEqual(
                audience_left,
                mapped_top_left[0] / mosaic.width,
            )
            self.assertAlmostEqual(
                audience_left + audience_width,
                mapped_bottom_right[0] / mosaic.width,
            )
            self.assertEqual("reference_ptz", mosaic.coordinate_space)
            self.assertGreaterEqual(len(mosaic.warp_mesh.triangles), 100)
            map_x, map_y, valid = mosaic.warp_mesh.reference_maps(mosaic)
            self.assertEqual((mosaic.height, mosaic.width), map_x.shape)
            self.assertEqual(map_x.shape, map_y.shape)
            components, _labels = cv2.connectedComponents(valid)
            self.assertEqual(2, components)
            self.assertGreater(np.count_nonzero(valid), valid.size * 0.25)

    def test_standard_video_preview_reserves_a_sixteen_by_nine_area(self) -> None:
        host = QWidget()
        layout = QVBoxLayout(host)
        preview = VideoPreview("empty")
        layout.addWidget(preview)
        layout.addStretch()
        host.resize(800, 700)
        host.show()
        for _ in range(3):
            APP.processEvents()

        self.assertAlmostEqual(16.0 / 9.0, preview.width() / preview.height(), places=2)
        flexible = VideoPreview("empty", aspect_ratio=None)
        self.assertFalse(flexible.hasHeightForWidth())
        host.close()
        flexible.close()

    def test_click_to_frame_keeps_rendering_live_audience_without_a_lock(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_review_fixture(root)
            review = load_latest_calibration_review(root)
            self.assertIsNotNone(review)
            assert review is not None
            mosaic = ensure_calibration_mosaic(review, maximum_width=900)
            context = build_context(root)
            context.config.integrations.video.audience_auto_connect = False
            context.config.integrations.video.ptz_auto_connect = False
            with patch.object(CameraControlPage, "reload_click_to_frame_mosaic"):
                page = CameraControlPage(context)
            with patch(
                "production_hub.ui.pages.camera_control_page."
                "PtzGeometryModel.load_active_panorama",
                return_value=None,
            ):
                page._apply_click_to_frame_mosaic(mosaic)

            red = QImage(1000, 500, QImage.Format.Format_RGB32)
            red.fill(QColor("#dc3232"))
            context.video.broker.publish(
                VideoSourceKey.AUDIENCE,
                red,
                frame_rate=30.0,
            )
            page.click_to_frame_window.refresh_frame()
            first = page.click_to_frame_window.preview.image
            first_sequence = page.click_to_frame_window.preview.sequence
            self.assertIsNotNone(first)

            green = QImage(1000, 500, QImage.Format.Format_RGB32)
            green.fill(QColor("#32dc32"))
            context.video.broker.publish(
                VideoSourceKey.AUDIENCE,
                green,
                frame_rate=30.0,
            )
            page._click_composite_last_render_monotonic = 0.0
            page.click_to_frame_window.refresh_frame()
            second = page.click_to_frame_window.preview.image
            self.assertIsNotNone(second)
            self.assertGreater(
                page.click_to_frame_window.preview.sequence,
                first_sequence,
            )

            row, column = np.unravel_index(
                np.argmax(page._click_reference_alpha),
                page._click_reference_alpha.shape,
            )
            first_color = first.pixelColor(int(column), int(row))
            second_color = second.pixelColor(int(column), int(row))
            self.assertGreater(second_color.green(), first_color.green() + 100)
            self.assertLess(second_color.red(), first_color.red() - 100)

            page.close()
            context.video.shutdown()

    def test_complete_workflow_requires_explicit_movement_confirmation(self) -> None:
        self.assertEqual(2, calibration_workflow_main([]))

    def test_simple_camera_sync_progress_reports_sweep_position(self) -> None:
        with TemporaryDirectory() as directory:
            context = build_context(Path(directory))
            context.config.integrations.video.audience_auto_connect = False
            context.config.integrations.video.ptz_auto_connect = False
            page = CameraControlPage(context)

            page._update_simple_calibration_progress(
                "Step 2/4 · Running guarded PTZ movement sweep…\n"
                "[6/11] Moving to stage-right: pan=8000 tilt=8000 zoom=800\n"
            )

            self.assertGreater(page.simple_calibration_progress.value(), 40)
            self.assertIn("position 6 of 11", page.simple_calibration_status.text())
            page.close()
            context.video.shutdown()

    def test_click_target_fades_and_floating_window_uses_tracking_toggle(self) -> None:
        preview = VideoPreview("empty")
        image = QImage(320, 180, QImage.Format.Format_RGB32)
        image.fill(QColor("#182436"))
        preview.set_frame(image, 1)
        preview.set_frame_target((0.5, 0.5))
        preview.fade_frame_target(1000)
        for _ in range(21):
            preview._fade_frame_target_step()
        self.assertIsNone(preview._frame_target_point)
        preview.close()

        with TemporaryDirectory() as directory:
            context = build_context(Path(directory))
            context.config.integrations.video.audience_auto_connect = False
            context.config.integrations.video.ptz_auto_connect = False
            page = CameraControlPage(context)
            window = page.click_to_frame_window
            self.assertFalse(hasattr(window, "status"))
            window.tracking_state_provider = lambda: (True, True)
            window.refresh_tracking_button()
            self.assertEqual("Disable Subject Tracking", window.tracking_button.text())
            window.tracking_state_provider = lambda: (False, False)
            window.refresh_tracking_button()
            self.assertEqual("Enable Subject Tracking", window.tracking_button.text())
            self.assertFalse(window.isVisible())
            page.floating_click_button.click()
            APP.processEvents()
            self.assertTrue(window.isVisible())
            page.hide_click_to_frame_window()
            page.close()
            context.video.shutdown()

    def test_application_tools_menu_runs_guarded_calibration_action(self) -> None:
        with TemporaryDirectory() as directory:
            context = build_context(Path(directory))
            context.config.integrations.video.audience_auto_connect = False
            context.config.integrations.video.ptz_auto_connect = False
            context.config.ui.show_menu_bar_icon = False
            context.config.ui.keep_running_after_window_close = False
            window = MainWindow(context)
            actions = {
                action.text(): action for action in window.tools_menu.actions()
            }
            label = "Calibrate Camera Sync…"
            self.assertIn(label, actions)
            with patch(
                "production_hub.ui.pages.camera_control_page.QMessageBox.warning"
            ) as warning:
                actions[label].trigger()
                self.assertEqual(1, warning.call_count)
            APP.processEvents()
            self.assertFalse(window.camera_control_page.camera_calibration_dialog.isVisible())
            self.assertEqual(
                "Camera Control",
                window.page_names[window.stack.currentIndex()],
            )
            window._quitting = True
            window.close()
            context.video.shutdown()

    @staticmethod
    def _write_review_fixture(root: Path) -> None:
        reference_directory = root / "calibration" / "reference"
        sweep_directory = root / "calibration-sweeps" / "fixture"
        moved_directory = sweep_directory / "02-wide-left"
        reference_directory.mkdir(parents=True)
        moved_directory.mkdir(parents=True)
        for path, color in (
            (reference_directory / "audience.jpg", "#425b72"),
            (reference_directory / "ptz.jpg", "#4b6351"),
            (moved_directory / "ptz.jpg", "#5b5148"),
        ):
            image = QImage(1000, 500, QImage.Format.Format_RGB32)
            image.fill(QColor(color))
            image.save(str(path))
        reference = {
            "created_at": "2026-08-08T20:00:00+00:00",
            "alignment": {
                "status": "accepted",
                "audience_size": [1000, 500],
                "ptz_size": [1000, 500],
                "inliers": 2,
                "median_error_pixels": 0.5,
                "audience_to_ptz": [[1, 0, 100], [0, 1, 0], [0, 0, 1]],
                "ptz_to_audience": [[1, 0, -100], [0, 1, 0], [0, 0, 1]],
                "correspondences": [
                    {
                        "audience_x": 100,
                        "audience_y": 100,
                        "ptz_x": 200,
                        "ptz_y": 100,
                        "error_pixels": 0.4,
                    },
                    {
                        "audience_x": 400,
                        "audience_y": 200,
                        "ptz_x": 500,
                        "ptz_y": 200,
                        "error_pixels": 0.6,
                    },
                ],
            },
            "artifacts": {"audience_image": "audience.jpg", "ptz_image": "ptz.jpg"},
        }
        (reference_directory / "calibration.json").write_text(
            json.dumps(reference),
            encoding="utf-8",
        )
        sync = {
            "created_at": "2026-08-08T20:10:00+00:00",
            "status": "accepted",
            "reference_calibration": str(reference_directory / "calibration.json"),
            "poses": [
                {
                    "index": 1,
                    "name": "reference",
                    "status": "accepted",
                    "reference_ptz_to_pose": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                    "motor_position": {"pan": 0x8000, "tilt": 0x8000, "zoom": 0x800},
                },
                {
                    "index": 2,
                    "name": "wide-left",
                    "status": "accepted",
                    "pose_ptz_image": str(moved_directory / "ptz.jpg"),
                    "reference_ptz_to_pose": [[1, 0, 50], [0, 1, 0], [0, 0, 1]],
                    "motor_position": {"pan": 0x7A00, "tilt": 0x8000, "zoom": 0x700},
                    "ptz_link": {
                        "ptz_size": [1000, 500],
                        "inliers": 80,
                        "median_error_pixels": 1.1,
                    },
                },
            ],
        }
        (sweep_directory / "full_sync.json").write_text(
            json.dumps(sync),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
