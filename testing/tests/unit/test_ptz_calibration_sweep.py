from __future__ import annotations

import unittest

import cv2
import numpy as np

from production_hub.calibration.ptz_sweep import (
    PtzAbsolutePose,
    build_bounded_stage_sweep,
    build_structural_landmark_sweep,
)
from production_hub.calibration.structural_markers import (
    assign_global_marker_ids,
    marker_atlas,
    select_structural_markers,
)
from production_hub.calibration.sync_map import (
    compose_audience_to_pose,
    invert_homography,
)


class PtzCalibrationSweepTests(unittest.TestCase):
    def test_bounded_sweep_starts_at_return_pose_and_stays_conservative(self) -> None:
        start = PtzAbsolutePose("return-position", 0xAAC1, 0x8494, 0x8BE)
        poses = build_bounded_stage_sweep(start)

        self.assertEqual(9, len(poses))
        self.assertEqual((start.pan, start.tilt, start.zoom), (
            poses[0].pan,
            poses[0].tilt,
            poses[0].zoom,
        ))
        self.assertLessEqual(max(abs(item.pan - start.pan) for item in poses), 0x0500)
        self.assertLessEqual(max(abs(item.tilt - start.tilt) for item in poses), 0x0180)
        self.assertTrue(all(0x555 <= item.zoom <= 0xFFF for item in poses))

    def test_sync_map_composes_reference_and_pose_homographies_in_order(self) -> None:
        audience_to_reference = (
            (2.0, 0.0, 10.0),
            (0.0, 2.0, 20.0),
            (0.0, 0.0, 1.0),
        )
        reference_to_pose = (
            (1.0, 0.0, 30.0),
            (0.0, 1.0, 40.0),
            (0.0, 0.0, 1.0),
        )
        composed = compose_audience_to_pose(
            audience_to_reference,
            reference_to_pose,
        )
        expected = np.asarray(reference_to_pose) @ np.asarray(audience_to_reference)
        np.testing.assert_allclose(np.asarray(composed), expected)
        np.testing.assert_allclose(
            np.asarray(invert_homography(composed)) @ np.asarray(composed),
            np.eye(3),
            atol=1e-9,
        )

    def test_structural_sweep_covers_wide_and_foreground_regions(self) -> None:
        start = PtzAbsolutePose("return-position", 0xAAC1, 0x8494, 0x8BE)
        poses = build_structural_landmark_sweep(start)

        self.assertEqual(11, len(poses))
        self.assertEqual("reference", poses[0].name)
        self.assertEqual("foreground-center", poses[-1].name)
        self.assertEqual(start.tilt + 0x0900, poses[-1].tilt)
        self.assertLessEqual(min(item.pan for item in poses), start.pan - 0x0800)
        self.assertGreaterEqual(max(item.pan for item in poses), start.pan + 0x0800)
        self.assertEqual(0x555, poses[1].zoom)

    def test_structural_markers_prefer_repeatability_and_spatial_coverage(self) -> None:
        image = np.zeros((600, 800, 3), dtype=np.uint8)
        correspondences = []
        expected = [(80, 70), (390, 90), (710, 80), (90, 510), (400, 500), (700, 520)]
        for index, (x, y) in enumerate(expected):
            cv2.rectangle(image, (x - 12, y - 12), (x + 12, y + 12), (255, 255, 255), 2)
            for sample in range(3):
                correspondences.append(
                    {
                        "audience_x": x + sample * 0.5,
                        "audience_y": y + sample * 0.25,
                        "ptz_x": 100 + index * 60 + sample * 0.5,
                        "ptz_y": 120 + index * 35 + sample * 0.25,
                        "error_pixels": 0.5 + sample * 0.1,
                    }
                )
        markers = select_structural_markers(correspondences, image, maximum_markers=12)
        self.assertEqual(6, len(markers))
        self.assertTrue(all(item["repeatability"] == 3 for item in markers))
        self.assertLess(min(item["audience_y"] for item in markers), 100)
        self.assertGreater(max(item["audience_y"] for item in markers), 500)

        poses = [
            {"structural_markers": [dict(item) for item in markers[:3]]},
            {"structural_markers": [dict(item) for item in markers[2:]]},
        ]
        count = assign_global_marker_ids(poses)
        self.assertEqual(6, count)
        self.assertEqual(6, len(marker_atlas(poses)))


if __name__ == "__main__":
    unittest.main()
