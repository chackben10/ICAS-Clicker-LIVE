"""Self-checks for the PTZ tracking simulator.

A simulator that quietly disagrees with the production geometry, or that wires
a motor sign backwards, would turn every recording built on it into confident
nonsense. These tests pin the properties the rest of the PTZ suites depend on.
"""

from __future__ import annotations

import unittest

from production_hub.tracking.ptz_geometry import PtzMotorPose
from testing.harness.ptz_tracking import (
    build_geometry,
    person,
    pose_framing,
    ptz_image_rects,
    ptz_view_box,
    reference_rect_to_ptz_image,
    run_scenario,
    scenario,
)


class PtzTrackingHarnessTests(unittest.TestCase):
    def test_image_projection_round_trips_through_production_geometry(self) -> None:
        geometry = build_geometry()
        subject = person("speaker", x=0.34)
        pose = pose_framing([subject], occupancy=0.83)
        image_rect = ptz_image_rects([subject], pose, geometry)[0]
        recovered = geometry.ptz_rect_to_reference(image_rect, pose)
        for expected, actual in zip(
            (subject.rect.x, subject.rect.y, subject.rect.width, subject.rect.height),
            (recovered.x, recovered.y, recovered.width, recovered.height),
        ):
            self.assertAlmostEqual(expected, actual, places=6)

    def test_operator_framing_centres_the_subject_at_the_requested_occupancy(self) -> None:
        subject = person("speaker", x=0.34)
        pose = pose_framing([subject], occupancy=0.80)
        image_rect = ptz_image_rects([subject], pose)[0]
        self.assertAlmostEqual(0.50, image_rect.center[0], places=3)
        self.assertAlmostEqual(0.50, image_rect.center[1], places=3)
        self.assertAlmostEqual(0.80, image_rect.height, places=3)

    def test_a_well_framed_subject_produces_no_motion(self) -> None:
        subject = person("speaker", x=0.34)
        recording = run_scenario(
            scenario(
                "still",
                [[subject]] * 8,
                initial_pose=pose_framing([subject], occupancy=0.83),
            )
        )
        self.assertTrue(recording.armed)
        for step in recording.steps:
            self.assertEqual((50, 50, 50), (step.pan_speed, step.tilt_speed, step.zoom_speed))

    def test_the_closed_loop_converges_on_an_off_centre_subject(self) -> None:
        """Guards the pan and tilt sign conventions.

        Panasonic drives the absolute pan coordinate downward for an AW speed
        above 50. If the harness calibration ever encodes the opposite, the
        image-space controller would accelerate away from the subject and every
        recording would capture a runaway instead of tracking.
        """

        framed = person("speaker", x=0.34)
        actual = person("speaker", x=0.385, floor=0.66)
        recording = run_scenario(
            scenario(
                "converge",
                [[actual]] * 16,
                initial_pose=pose_framing([framed], occupancy=0.83),
            )
        )
        offsets = [
            abs(step.ptz_detections[0][0] + step.ptz_detections[0][2] / 2.0 - 0.50)
            for step in recording.steps
            if step.ptz_detections
        ]
        self.assertGreater(offsets[0], 0.20, "the subject should start well off centre")
        self.assertLess(offsets[-1], 0.07, f"the loop did not converge: {offsets}")
        self.assertLess(offsets[-1], offsets[0])

    def test_the_simulated_camera_reaches_both_ends_of_its_zoom_range(self) -> None:
        geometry = build_geometry()
        minimum, maximum = geometry.minimum_zoom, geometry.maximum_zoom
        widest = ptz_view_box(geometry, PtzMotorPose(44000, 34000, minimum))
        tightest = ptz_view_box(geometry, PtzMotorPose(44000, 34000, maximum))
        self.assertGreater(widest.height, 0.60)
        self.assertLess(tightest.height, 0.05, "the harness camera cannot frame a head")
        self.assertAlmostEqual(widest.width, widest.height, places=6)

    def test_a_person_outside_the_view_is_not_reported_by_the_ptz_detector(self) -> None:
        geometry = build_geometry()
        subject = person("speaker", x=0.34)
        pose = pose_framing([subject], occupancy=0.83)
        view = ptz_view_box(geometry, pose)
        elsewhere = person("other", x=0.80)
        image_rect = reference_rect_to_ptz_image(elsewhere.rect, view)
        self.assertTrue(image_rect.x > 1.0 or image_rect.x + image_rect.width < 0.0)


if __name__ == "__main__":
    unittest.main()
