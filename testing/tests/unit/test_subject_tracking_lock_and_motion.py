from __future__ import annotations

import unittest

from production_hub.calibration.relocalization import (
    RelocalizationSnapshot,
    RelocalizationState,
)
from production_hub.core.config.models import CameraTrackingConfig
from production_hub.tracking.models import (
    NormalizedRect,
    PersonCandidate,
    TrackedSubject,
    TrackingSnapshot,
    TrackingState,
)
from production_hub.tracking.ptz_geometry import PtzMotorPose
from production_hub.tracking.service import PersonTrackingService
from production_hub.tracking.target_lock import SubjectTargetLock
from production_hub.tracking.velocity_controller import SmoothPtzVelocityController
from production_hub.video.frame_broker import LatestFrameBroker
from production_hub.video.models import VideoSourceKey


class _IdentityGeometry:
    def ptz_rect_to_reference(self, bounds, _pose):
        return bounds


def _subject(
    track_id: int,
    bounds: NormalizedRect,
    now: float,
    *,
    age: int = 6,
) -> TrackedSubject:
    return TrackedSubject(track_id, bounds, 0.92, False, age, now)


def _snapshot(
    source: VideoSourceKey,
    now: float,
    *subjects: TrackedSubject,
) -> TrackingSnapshot:
    return TrackingSnapshot(
        source=source,
        state=TrackingState.RUNNING,
        subjects=tuple(subjects),
        last_analysis_monotonic=now,
    )


def _locked_relocalization() -> RelocalizationSnapshot:
    return RelocalizationSnapshot(
        state=RelocalizationState.LOCKED,
        approval_status="approved",
        live_size=(1000, 1000),
        reference_size=(1000, 1000),
    )


class SubjectTargetLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lock = SubjectTargetLock(maximum_match_distance=0.30)
        self.geometry = _IdentityGeometry()
        self.pose = PtzMotorPose(43000, 34000, 2200)
        self.relocalization = _locked_relocalization()

    def _update(self, now, ptz_subjects=(), audience_subjects=()):
        return self.lock.update(
            _snapshot(VideoSourceKey.PTZ, now, *ptz_subjects),
            _snapshot(VideoSourceKey.AUDIENCE, now, *audience_subjects),
            self.relocalization,
            self.geometry,
            self.pose,
            minimum_age_frames=2,
            now=now,
        )

    def test_new_people_are_never_added_after_arm_time(self) -> None:
        first = self._update(
            10.0,
            ptz_subjects=(
                _subject(7, NormalizedRect(0.16, 0.12, 0.12, 0.50), 10.0),
                _subject(8, NormalizedRect(0.68, 0.10, 0.12, 0.52), 10.0),
            ),
        )
        self.assertEqual(2, first.lock_count)

        updated = self._update(
            10.25,
            ptz_subjects=(
                _subject(7, NormalizedRect(0.18, 0.12, 0.12, 0.50), 10.25),
                _subject(99, NormalizedRect(0.44, 0.10, 0.12, 0.52), 10.25),
                _subject(8, NormalizedRect(0.66, 0.10, 0.12, 0.52), 10.25),
            ),
        )

        self.assertEqual(2, updated.lock_count)
        self.assertEqual(2, len(updated.ptz.subjects))
        self.assertFalse(
            any(abs(item.bounds.center[0] - 0.50) < 0.03 for item in updated.ptz.subjects)
        )

    def test_detector_id_can_change_after_dropout_without_changing_lock(self) -> None:
        original = NormalizedRect(0.34, 0.12, 0.15, 0.54)
        first = self._update(
            20.0,
            ptz_subjects=(_subject(3, original, 20.0),),
        )
        logical_id = first.ptz.subjects[0].track_id

        missing = self._update(25.0)
        self.assertTrue(missing.initialized)
        self.assertEqual((), missing.ptz.subjects)

        reacquired = self._update(
            31.5,
            ptz_subjects=(
                _subject(41, NormalizedRect(0.38, 0.13, 0.15, 0.54), 31.5, age=1),
                _subject(42, NormalizedRect(0.82, 0.12, 0.12, 0.48), 31.5),
            ),
        )
        self.assertEqual(1, len(reacquired.ptz.subjects))
        self.assertEqual(logical_id, reacquired.ptz.subjects[0].track_id)

    def test_audience_match_can_follow_locked_person_outside_stage(self) -> None:
        bounds = NormalizedRect(0.40, 0.12, 0.13, 0.54)
        first = self._update(
            30.0,
            ptz_subjects=(_subject(5, bounds, 30.0),),
            audience_subjects=(_subject(20, bounds, 30.0),),
        )
        self.assertEqual(0, first.audience_matches)

        bound = self._update(
            30.25,
            ptz_subjects=(_subject(5, bounds, 30.25),),
            audience_subjects=(_subject(20, bounds, 30.25),),
        )
        self.assertEqual(1, bound.audience_matches)

        followed = bound
        for index in range(1, 9):
            now = 30.25 + index * 0.25
            moved = NormalizedRect(0.40 - index * 0.012, 0.12 + index * 0.025, 0.13, 0.54)
            followed = self._update(
                now,
                audience_subjects=(
                    _subject(20, moved, now),
                    _subject(21, NormalizedRect(0.85, 0.60, 0.10, 0.28), now),
                ),
            )

        self.assertEqual(1, len(followed.audience.subjects))
        self.assertEqual(1, followed.audience.subjects[0].track_id)

    def test_bound_audience_id_cannot_jump_to_a_corner(self) -> None:
        bounds = NormalizedRect(0.40, 0.12, 0.13, 0.54)
        self._update(
            40.0,
            ptz_subjects=(_subject(5, bounds, 40.0),),
            audience_subjects=(_subject(20, bounds, 40.0),),
        )
        self._update(
            40.25,
            ptz_subjects=(_subject(5, bounds, 40.25),),
            audience_subjects=(_subject(20, bounds, 40.25),),
        )

        jumped = self._update(
            40.50,
            audience_subjects=(
                _subject(20, NormalizedRect(0.02, 0.68, 0.13, 0.30), 40.50),
            ),
        )

        self.assertEqual(0, jumped.audience_matches)
        self.assertEqual((), jumped.audience.subjects)
        self.assertEqual((), jumped.reference_targets)

    def test_audience_id_can_renew_only_along_the_locked_motion_path(self) -> None:
        bounds = NormalizedRect(0.40, 0.12, 0.13, 0.54)
        self._update(
            50.0,
            ptz_subjects=(_subject(5, bounds, 50.0),),
            audience_subjects=(_subject(20, bounds, 50.0),),
        )
        self._update(
            50.25,
            ptz_subjects=(_subject(5, bounds, 50.25),),
            audience_subjects=(_subject(20, bounds, 50.25),),
        )

        first_new_id = self._update(
            50.50,
            audience_subjects=(
                _subject(91, NormalizedRect(0.39, 0.14, 0.13, 0.54), 50.50),
            ),
        )
        renewed = self._update(
            50.75,
            audience_subjects=(
                _subject(91, NormalizedRect(0.38, 0.16, 0.13, 0.54), 50.75),
            ),
        )

        self.assertEqual(0, first_new_id.audience_matches)
        self.assertEqual(1, renewed.audience_matches)
        self.assertEqual(1, renewed.audience.subjects[0].track_id)

    def test_proven_audience_identity_coasts_through_short_detector_dropout(self) -> None:
        first_bounds = NormalizedRect(0.40, 0.12, 0.13, 0.54)
        moved_bounds = NormalizedRect(0.43, 0.12, 0.13, 0.54)
        self._update(
            60.0,
            ptz_subjects=(_subject(5, first_bounds, 60.0),),
            audience_subjects=(_subject(20, first_bounds, 60.0),),
        )
        bound = self._update(
            60.25,
            ptz_subjects=(_subject(5, moved_bounds, 60.25),),
            audience_subjects=(_subject(20, moved_bounds, 60.25),),
        )
        self.assertEqual(1, bound.audience_matches)

        coast = self._update(60.55)

        self.assertEqual(0, coast.audience_matches)
        self.assertEqual(1, coast.audience_coasts)
        self.assertEqual(1, len(coast.reference_targets))
        self.assertEqual("audience", coast.reference_targets[0].source)
        self.assertGreater(
            coast.reference_targets[0].bounds.center[0],
            bound.reference_targets[0].bounds.center[0],
        )

        expired = self._update(61.80)
        self.assertEqual(0, expired.audience_coasts)
        self.assertEqual((), expired.reference_targets)

    def test_unverified_audience_identity_never_enables_prediction(self) -> None:
        first_bounds = NormalizedRect(0.40, 0.12, 0.13, 0.54)
        moved_bounds = NormalizedRect(0.43, 0.12, 0.13, 0.54)
        self._update(
            70.0,
            ptz_subjects=(_subject(5, first_bounds, 70.0),),
        )
        self._update(
            70.25,
            ptz_subjects=(_subject(5, moved_bounds, 70.25),),
        )

        missing = self._update(70.55)

        self.assertEqual(0, missing.audience_coasts)
        self.assertEqual((), missing.reference_targets)


class SmoothVelocityControllerTests(unittest.TestCase):
    def test_audience_motor_pan_uses_panasonic_absolute_coordinate_sign(self) -> None:
        config = CameraTrackingConfig(enabled=True).automation
        actual = PtzMotorPose(43000, 34000, 2200)

        toward_higher_position = SmoothPtzVelocityController().update(
            actual=actual,
            desired=PtzMotorPose(44600, 34000, 2200),
            config=config,
            now=0.0,
            live_bounds=None,
        )
        toward_lower_position = SmoothPtzVelocityController().update(
            actual=actual,
            desired=PtzMotorPose(41400, 34000, 2200),
            config=config,
            now=0.0,
            live_bounds=None,
        )

        self.assertLess(toward_higher_position.pan, 50)
        self.assertGreater(toward_lower_position.pan, 50)

    def test_slow_walk_produces_slew_limited_continuous_pan(self) -> None:
        config = CameraTrackingConfig(enabled=True).automation
        controller = SmoothPtzVelocityController()
        actual = PtzMotorPose(43000, 34000, 2200)
        desired = PtzMotorPose(44000, 34000, 2200)
        speeds = []
        for index in range(8):
            command = controller.update(
                actual=actual,
                desired=desired,
                config=config,
                now=index * 0.25,
                live_bounds=NormalizedRect(0.63, 0.20, 0.12, 0.55),
            )
            speeds.append(command.pan)

        self.assertGreater(speeds[-1], 50)
        self.assertTrue(all(right >= left for left, right in zip(speeds, speeds[1:])))
        self.assertTrue(all(right - left <= 5 for left, right in zip(speeds, speeds[1:])))

    def test_subject_motion_prediction_accelerates_pan_before_the_edge(self) -> None:
        config = CameraTrackingConfig(enabled=True).automation
        config.continuous_speed_slew_per_second = 30.0
        actual = PtzMotorPose(43000, 34000, 2200)
        desired = PtzMotorPose(44000, 34000, 2200)
        moving = SmoothPtzVelocityController()
        stationary = SmoothPtzVelocityController()

        moving.update(
            actual=actual,
            desired=desired,
            config=config,
            now=0.0,
            live_bounds=NormalizedRect(0.44, 0.20, 0.12, 0.55),
        )
        moving.update(
            actual=actual,
            desired=desired,
            config=config,
            now=0.25,
            live_bounds=NormalizedRect(0.60, 0.20, 0.12, 0.55),
        )
        moving_command = moving.update(
            actual=actual,
            desired=desired,
            config=config,
            now=0.50,
            live_bounds=NormalizedRect(0.72, 0.20, 0.12, 0.55),
        )

        stationary_bounds = (
            NormalizedRect(0.44, 0.20, 0.12, 0.55),
            NormalizedRect(0.44, 0.20, 0.12, 0.55),
            NormalizedRect(0.72, 0.20, 0.12, 0.55),
        )
        for index, bounds in enumerate(stationary_bounds):
            stationary_command = stationary.update(
                actual=actual,
                desired=desired,
                config=config,
                now=index * 0.25,
                live_bounds=bounds,
            )

        self.assertGreater(moving_command.pan, stationary_command.pan)
        self.assertGreaterEqual(moving_command.pan, 64)

    def test_second_recording_walk_reaches_catchup_speed_before_edge(self) -> None:
        config = CameraTrackingConfig(enabled=True).automation
        controller = SmoothPtzVelocityController()
        pose = PtzMotorPose(43000, 34000, 2200)
        # Compact trace derived from 5.0-7.0s of the supplied recording.
        centers = (0.549, 0.610, 0.668, 0.710, 0.755, 0.810, 0.863, 0.900, 0.938)
        speeds = []
        for index, center_x in enumerate(centers):
            command = controller.update(
                actual=pose,
                desired=pose,
                config=config,
                now=index * 0.25,
                live_bounds=NormalizedRect(center_x - 0.05, 0.20, 0.10, 0.55),
            )
            speeds.append(command.pan)

        self.assertGreaterEqual(speeds[-3], 74)
        self.assertGreaterEqual(len(set(speeds)), 6)
        self.assertTrue(all(right - left <= 5 for left, right in zip(speeds, speeds[1:])))

    def test_saved_revision_one_velocity_limits_are_migrated(self) -> None:
        config = CameraTrackingConfig.from_dict(
            {
                "enabled": True,
                "automation": {
                    "continuous_control_revision": 1,
                    "continuous_pan_max_speed": 13,
                    "continuous_tilt_max_speed": 9,
                    "continuous_zoom_max_speed": 5,
                    "continuous_speed_slew_per_second": 10.0,
                    "continuous_error_smoothing": 0.38,
                },
            }
        ).automation

        self.assertEqual(2, config.continuous_control_revision)
        self.assertEqual(26, config.continuous_pan_max_speed)
        self.assertEqual(14, config.continuous_tilt_max_speed)
        self.assertEqual(8, config.continuous_zoom_max_speed)

    def test_centered_subject_decelerates_to_neutral_without_reversing(self) -> None:
        config = CameraTrackingConfig(enabled=True).automation
        controller = SmoothPtzVelocityController()
        actual = PtzMotorPose(43000, 34000, 2200)
        desired = PtzMotorPose(43000, 34000, 2200)
        controller.update(
            actual=actual,
            desired=PtzMotorPose(44000, 34000, 2200),
            config=config,
            now=0.0,
            live_bounds=NormalizedRect(0.70, 0.20, 0.12, 0.55),
        )
        speeds = []
        for index in range(1, 21):
            command = controller.update(
                actual=actual,
                desired=desired,
                config=config,
                now=index * 0.25,
                live_bounds=NormalizedRect(0.44, 0.20, 0.12, 0.55),
            )
            speeds.append(command.pan)

        self.assertTrue(all(speed >= 50 for speed in speeds))
        self.assertEqual(50, speeds[-1])


class SubjectModeAudienceCoverageTests(unittest.TestCase):
    def test_subject_mode_does_not_crop_audience_to_stage_regions(self) -> None:
        config = CameraTrackingConfig(enabled=True)
        config.automation.mode = "subject"
        service = PersonTrackingService(LatestFrameBroker(), config)
        candidates = [
            PersonCandidate(NormalizedRect(0.20, 0.10, 0.10, 0.20), 0.9),
            PersonCandidate(NormalizedRect(0.65, 0.70, 0.10, 0.20), 0.9),
        ]

        filtered, regions = service._filter_candidates(
            VideoSourceKey.AUDIENCE,
            candidates,
            available_regions=(),
        )

        self.assertEqual(candidates, filtered)
        self.assertEqual((), regions)


if __name__ == "__main__":
    unittest.main()
