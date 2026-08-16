from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover - dependency installation guard
    cv2 = None


@unittest.skipIf(cv2 is None, "opencv-python-headless is not installed")
class CameraAlignmentPhaseThreeTests(unittest.TestCase):
    def test_recovers_known_perspective_transform_from_natural_features(self) -> None:
        from production_hub.calibration import (
            AlignmentSettings,
            consolidate_alignments,
            estimate_alignment,
            render_alignment_diagnostics,
        )

        audience = self._textured_scene()
        source_corners = np.float32([[0, 0], [999, 0], [999, 639], [0, 639]])
        target_corners = np.float32([[72, 54], [899, 27], [948, 605], [42, 618]])
        expected = cv2.getPerspectiveTransform(source_corners, target_corners)
        ptz = cv2.warpPerspective(audience, expected, (1000, 640))

        result = estimate_alignment(
            audience,
            ptz,
            AlignmentSettings(
                maximum_width=1200,
                maximum_features=6000,
                minimum_matches=25,
                minimum_inliers=20,
            ),
        )

        self.assertTrue(result.accepted, result.reasons)
        self.assertGreater(result.inliers, 100)
        self.assertLess(result.median_error_pixels, 1.5)
        sample = np.float32(
            [[[120, 100]], [[500, 110]], [[850, 180]], [[260, 510]], [[720, 540]]]
        )
        expected_points = cv2.perspectiveTransform(sample, expected)
        actual_points = cv2.perspectiveTransform(
            sample,
            np.asarray(result.audience_to_ptz, dtype=np.float64),
        )
        error = np.linalg.norm(expected_points[:, 0] - actual_points[:, 0], axis=1)
        self.assertLess(float(np.median(error)), 1.0)

        consolidated = consolidate_alignments([result, result])
        self.assertTrue(consolidated.accepted, consolidated.reasons)
        self.assertGreaterEqual(consolidated.inliers, result.inliers * 2)
        self.assertIn("multi-sample consensus", consolidated.method)

        with TemporaryDirectory() as directory:
            artifacts = render_alignment_diagnostics(
                audience,
                ptz,
                result,
                Path(directory),
            )
            self.assertEqual(
                {
                    "audience_image",
                    "ptz_image",
                    "inlier_matches_image",
                    "alignment_overlay_image",
                },
                set(artifacts),
            )
            self.assertTrue(all((Path(directory) / name).is_file() for name in artifacts.values()))

    def test_rejects_images_without_features(self) -> None:
        from production_hub.calibration import AlignmentError, estimate_alignment

        blank = np.zeros((480, 640, 3), dtype=np.uint8)
        with self.assertRaises(AlignmentError):
            estimate_alignment(blank, blank)

    @staticmethod
    def _textured_scene() -> np.ndarray:
        random = np.random.default_rng(20260808)
        image = np.full((640, 1000, 3), 24, dtype=np.uint8)
        for index in range(650):
            center = (
                int(random.integers(10, image.shape[1] - 10)),
                int(random.integers(10, image.shape[0] - 10)),
            )
            color = tuple(int(value) for value in random.integers(55, 255, 3))
            radius = int(random.integers(2, 8))
            cv2.circle(image, center, radius, color, -1, cv2.LINE_AA)
            if index % 4 == 0:
                end = (
                    min(image.shape[1] - 1, center[0] + int(random.integers(-25, 26))),
                    min(image.shape[0] - 1, center[1] + int(random.integers(-25, 26))),
                )
                cv2.line(image, center, end, color, 2, cv2.LINE_AA)
        for row in range(4):
            for column in range(7):
                cv2.putText(
                    image,
                    f"MARKER {row}-{column}",
                    (25 + column * 135, 75 + row * 145),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.48,
                    (235, 235, 235),
                    1,
                    cv2.LINE_AA,
                )
        return image


if __name__ == "__main__":
    unittest.main()
