from __future__ import annotations

import asyncio
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fastapi.testclient import TestClient
from PySide6.QtGui import QColor, QImage

from production_hub.api.server import create_app
from production_hub.app.bootstrap import build_context
from production_hub.calibration.relocalization import (
    RelocalizationSnapshot,
    RelocalizationState,
)
from production_hub.core.config.models import (
    CameraSceneRegion,
    CameraTrackingConfig,
    SceneRegionPoint,
)
from production_hub.tracking.framing import (
    FramingDecision,
    FramingEngine,
    FramingState,
    _adaptive_ptz_composition,
)
from production_hub.tracking.apple_vision import (
    _raised_hand_extension_points,
    merge_pose_envelopes,
)
from production_hub.tracking.models import (
    NormalizedRect,
    PersonCandidate,
    TrackedSubject,
    TrackingSnapshot,
    TrackingState,
)
from production_hub.tracking.ptz_automation import (
    AutomationState,
    PtzAutomationService,
    _relative_handoff_pose,
    _target_is_continuous,
)
from production_hub.tracking.ptz_geometry import (
    CalibrationPoseAnchor,
    PtzGeometryModel,
    PtzMotorPose,
)
from production_hub.tracking.service import PersonTrackingService
from production_hub.tracking.velocity_controller import PtzVelocityCommand
from production_hub.video.frame_broker import LatestFrameBroker
from production_hub.video.models import (
    VideoSourceKey,
    VideoSourceSnapshot,
    VideoSourceState,
)


def _image() -> QImage:
    image = QImage(640, 360, QImage.Format.Format_RGB32)
    image.fill(QColor("#182436"))
    return image


def _stage(kind: str = "stage", *, name: str = "Stage") -> CameraSceneRegion:
    return CameraSceneRegion(
        id=kind,
        name=name,
        kind=kind,
        points=[
            SceneRegionPoint(0.10, 0.05),
            SceneRegionPoint(0.90, 0.05),
            SceneRegionPoint(0.90, 0.50),
            SceneRegionPoint(0.10, 0.50),
        ],
    )


def _locked() -> RelocalizationSnapshot:
    return RelocalizationSnapshot(
        state=RelocalizationState.LOCKED,
        approval_status="approved",
        reference_size=(1000, 1000),
        live_size=(1000, 1000),
    )


class _Detector:
    backend_name = "Test"

    def detect(self, _image: QImage) -> list[PersonCandidate]:
        return [
            PersonCandidate(NormalizedRect(0.25, 0.10, 0.10, 0.20), 0.92),
            PersonCandidate(NormalizedRect(0.60, 0.60, 0.10, 0.25), 0.90),
        ]


class _Geometry:
    map_path = Path("/approved/full_sync.json")

    def __init__(self) -> None:
        self.frame_heights: list[float] = []
        self.relative_footprints: list[float] = []

    def pose_for_target(self, _target, *, desired_frame_height: float) -> PtzMotorPose:
        self.frame_heights.append(desired_frame_height)
        return PtzMotorPose(45000, 34500, 3000)

    def ptz_rect_to_reference(self, bounds, _pose):
        return bounds

    def zoom_for_relative_footprint(self, current_zoom, relative_footprint):
        self.relative_footprints.append(relative_footprint)
        return round(current_zoom + (1.0 - relative_footprint) * 1000)


class _Tracking:
    def __init__(self, audience: TrackingSnapshot, ptz: TrackingSnapshot) -> None:
        self.values = {
            VideoSourceKey.AUDIENCE: audience,
            VideoSourceKey.PTZ: ptz,
        }

    def snapshot(self, source: VideoSourceKey) -> TrackingSnapshot:
        return self.values[source].copy()


class _Relocalization:
    def __init__(self, snapshot: RelocalizationSnapshot) -> None:
        self.value = snapshot

    def snapshot(self) -> RelocalizationSnapshot:
        return self.value.copy()


class _Video:
    def __init__(self, audience: TrackingSnapshot, ptz: TrackingSnapshot) -> None:
        self.tracking = _Tracking(audience, ptz)
        self.relocalization = _Relocalization(_locked())
        self.activity: list[tuple[str, bool, str]] = []
        self.video_snapshot = VideoSourceSnapshot(
            VideoSourceKey.AUDIENCE,
            VideoSourceState.RUNNING,
            last_frame_monotonic=time.monotonic(),
        )

    def snapshot(self, _source: VideoSourceKey) -> VideoSourceSnapshot:
        return self.video_snapshot.copy()

    def set_tracking_activity(self, active: bool, *, owner: str = "ui") -> None:
        self.activity.append(("tracking", active, owner))

    def set_calibration_activity(self, active: bool, *, owner: str = "ui") -> None:
        self.activity.append(("calibration", active, owner))


class _Panasonic:
    def __init__(self) -> None:
        self.config = SimpleNamespace(enabled=True)
        self.pose = PtzMotorPose(43700, 33900, 2000)
        self.pan_tilt_commands: list[tuple[int, int]] = []
        self.zoom_commands: list[int] = []
        self.pan_tilt_speed_commands: list[tuple[int, int]] = []
        self.zoom_speed_commands: list[int] = []

    async def query_pan_tilt_position(self):
        return self.pose.pan, self.pose.tilt

    async def query_zoom_position(self):
        return self.pose.zoom

    async def absolute_pan_tilt(self, pan: int, tilt: int) -> bool:
        self.pan_tilt_commands.append((pan, tilt))
        self.pose = PtzMotorPose(pan, tilt, self.pose.zoom)
        return True

    async def absolute_zoom(self, zoom: int) -> bool:
        self.zoom_commands.append(zoom)
        self.pose = PtzMotorPose(self.pose.pan, self.pose.tilt, zoom)
        return True

    async def pan_tilt(self, pan: int, tilt: int) -> bool:
        self.pan_tilt_speed_commands.append((pan, tilt))
        return True

    async def stop_pan_tilt(self) -> bool:
        self.pan_tilt_speed_commands.append((50, 50))
        return True

    async def zoom(self, zoom: int) -> bool:
        self.zoom_speed_commands.append(zoom)
        return True

    async def stop_zoom(self) -> bool:
        self.zoom_speed_commands.append(50)
        return True


def _snapshot(source: VideoSourceKey, *subjects: TrackedSubject) -> TrackingSnapshot:
    return TrackingSnapshot(
        source=source,
        state=TrackingState.RUNNING,
        subjects=tuple(subjects),
        last_analysis_monotonic=time.monotonic(),
    )


class PtzAutomationPhase45Tests(unittest.TestCase):
    def test_audience_handoff_cancels_absolute_map_bias(self) -> None:
        # The calibration inverse is biased +900 pan / +500 tilt at both
        # points. Relative motion must preserve only the small target delta.
        handoff = _relative_handoff_pose(
            motor_anchor=PtzMotorPose(43000, 34000, 3000),
            mapped_anchor=PtzMotorPose(43900, 34500, 2500),
            mapped_target=PtzMotorPose(43980, 34440, 2600),
            current_zoom=3075,
        )

        self.assertEqual(PtzMotorPose(43080, 33940, 3075), handoff)

    def test_audience_handoff_continues_verified_direction_on_recorded_oscillation(self) -> None:
        config = CameraTrackingConfig(enabled=True)
        config.automation.mode = "subject"
        service = PtzAutomationService(
            _Video(
                _snapshot(VideoSourceKey.AUDIENCE),
                _snapshot(VideoSourceKey.PTZ),
            ),
            _Panasonic(),
            config,
            Path("/tmp"),
            geometry_loader=lambda _root: _Geometry(),
        )
        # The live failure left the PTZ frame with a verified left command,
        # then the wide estimate flipped from 43662 to 44099.
        service._last_velocity_command = PtzVelocityCommand(36, 50, 50)
        service._prepare_handoff_guard(PtzMotorPose(43916, 33467, 3606))

        searched, first_reason = service._guard_audience_handoff(
            PtzMotorPose(43662, 33795, 3606),
            actual=PtzMotorPose(43916, 33467, 3606),
            now=100.0,
        )
        accepted, accepted_reason = service._guard_audience_handoff(
            PtzMotorPose(44099, 33870, 3606),
            actual=PtzMotorPose(43962, 33471, 3606),
            now=100.85,
        )

        # AW speed 36 moved the live camera toward increasing absolute pan.
        # Reject the first noisy lower-coordinate estimate and continue in
        # that verified direction. The later higher-coordinate estimate is
        # then consistent with the physical motion and may take over.
        self.assertEqual(PtzMotorPose(46516, 33467, 3606), searched)
        self.assertIn("continuing the last PTZ-verified edge direction", first_reason)
        self.assertIsNotNone(accepted)
        self.assertEqual(44099, accepted.pan)
        self.assertEqual("", accepted_reason)

    def test_audience_handoff_is_bounded_by_travel_and_time(self) -> None:
        config = CameraTrackingConfig(enabled=True)
        config.automation.mode = "subject"
        service = PtzAutomationService(
            _Video(
                _snapshot(VideoSourceKey.AUDIENCE),
                _snapshot(VideoSourceKey.PTZ),
            ),
            _Panasonic(),
            config,
            Path("/tmp"),
            geometry_loader=lambda _root: _Geometry(),
        )
        service._last_velocity_command = PtzVelocityCommand(36, 50, 50)
        origin = PtzMotorPose(43000, 34000, 3200)
        service._prepare_handoff_guard(origin)

        bounded, reason = service._guard_audience_handoff(
            PtzMotorPose(47000, 34000, 3200),
            actual=origin,
            now=200.0,
        )
        expired, expired_reason = service._guard_audience_handoff(
            PtzMotorPose(44000, 34000, 3200),
            actual=PtzMotorPose(43800, 34000, 3200),
            now=214.1,
        )

        self.assertEqual(45600, bounded.pan)
        self.assertEqual("", reason)
        self.assertIsNone(expired)
        self.assertIn("safe time limit", expired_reason)

    def test_body_pose_envelope_expands_rectangle_for_raised_hand(self) -> None:
        candidates = [
            PersonCandidate(NormalizedRect(0.40, 0.25, 0.12, 0.30), 0.93)
        ]
        expanded = merge_pose_envelopes(
            candidates,
            [NormalizedRect(0.38, 0.08, 0.18, 0.72)],
        )
        self.assertEqual(1, len(expanded))
        self.assertLess(expanded[0].bounds.y, candidates[0].bounds.y)
        self.assertGreater(expanded[0].bounds.height, candidates[0].bounds.height)

    def test_body_pose_can_supply_a_distant_subject_without_a_rectangle(self) -> None:
        envelope = NormalizedRect(0.46, 0.08, 0.07, 0.24)

        candidates = merge_pose_envelopes([], [envelope])

        self.assertEqual(1, len(candidates))
        self.assertEqual(envelope, candidates[0].bounds)
        self.assertEqual(0.50, candidates[0].confidence)

    def test_raised_hand_extension_estimates_fingertips_beyond_vision_wrist(self) -> None:
        extension = _raised_hand_extension_points(
            {
                "left_shoulder_1_joint": (0.612, 0.380),
                "left_forearm_joint": (0.679, 0.319),
                "left_hand_joint": (0.725, 0.157),
            }
        )

        self.assertEqual(1, len(extension))
        self.assertLess(extension[0][1], 0.025)

    def test_reference_shots_preserve_good_zoom_and_reframe_clipped_hand(self) -> None:
        config = CameraTrackingConfig(enabled=True).automation
        good_samples = (
            ([NormalizedRect(0.2189, 0.1385, 0.5094, 0.7451)], True),
            ([NormalizedRect(0.3247, 0.0326, 0.4150, 0.8277)], True),
            ([NormalizedRect(0.2866, 0.0783, 0.2108, 0.8257)], False),
            (
                [
                    NormalizedRect(0.2444, 0.0346, 0.2208, 0.6941),
                    NormalizedRect(0.5929, 0.1208, 0.2813, 0.6717),
                ],
                False,
            ),
        )
        for subjects, podium in good_samples:
            target, desired = _adaptive_ptz_composition(
                config,
                subjects,
                podium=podium,
            )
            footprint = max(
                target.height / desired,
                target.width / (desired * 16.0 / 9.0),
            )
            self.assertAlmostEqual(1.0, footprint, delta=0.025)

        # IMG_2422 after fingertip extrapolation: tilt upward and widen enough
        # to establish useful safety space above the clipped gesture.
        target, desired = _adaptive_ptz_composition(
            config,
            [NormalizedRect(0.3390, 0.0, 0.4149, 0.9810)],
            podium=True,
        )
        footprint = max(
            target.height / desired,
            target.width / (desired * 16.0 / 9.0),
        )
        self.assertAlmostEqual(0.50, target.center[0], places=3)
        self.assertAlmostEqual(0.47, target.center[1], places=3)
        self.assertGreater(footprint, 1.10)
        self.assertEqual(config.raised_gesture_maximum_occupancy, desired)

    def test_live_ptz_zoom_uses_relative_composition_for_clipped_subject(self) -> None:
        subject = TrackedSubject(
            1,
            NormalizedRect(0.15, 0.0, 0.30, 1.0),
            0.90,
            False,
            5,
            time.monotonic(),
        )
        geometry = _Geometry()
        config = CameraTrackingConfig(enabled=True).automation
        config.mode = "subject"
        current = PtzMotorPose(43150, 33200, 3645)

        decision = FramingEngine().decide(
            config,
            _snapshot(VideoSourceKey.AUDIENCE),
            _snapshot(VideoSourceKey.PTZ, subject),
            _locked(),
            (_stage(),),
            geometry,
            current_pose=current,
        )

        self.assertEqual(FramingState.READY, decision.state)
        self.assertGreater(geometry.relative_footprints[-1], 1.0)
        self.assertLess(decision.desired_pose.zoom, current.zoom)

    def test_side_clipped_subject_widens_instead_of_zooming_into_partial_body(self) -> None:
        subject = TrackedSubject(
            1,
            NormalizedRect(0.0, 0.20, 0.08, 0.36),
            0.90,
            False,
            5,
            time.monotonic(),
        )
        geometry = _Geometry()
        config = CameraTrackingConfig(enabled=True).automation
        config.mode = "subject"
        current = PtzMotorPose(43150, 33200, 3645)

        decision = FramingEngine().decide(
            config,
            _snapshot(VideoSourceKey.AUDIENCE),
            _snapshot(VideoSourceKey.PTZ, subject),
            _locked(),
            (_stage(),),
            geometry,
            current_pose=current,
        )

        self.assertEqual(FramingState.READY, decision.state)
        self.assertGreaterEqual(geometry.relative_footprints[-1], 1.08)
        self.assertLess(decision.desired_pose.zoom, current.zoom)

    def test_centered_off_lectern_subject_tightens_to_reference_scale(self) -> None:
        subject = TrackedSubject(
            1,
            NormalizedRect(0.43, 0.20, 0.14, 0.56),
            0.92,
            False,
            5,
            time.monotonic(),
        )
        geometry = _Geometry()
        config = CameraTrackingConfig(enabled=True).automation
        config.mode = "subject"
        current = PtzMotorPose(43150, 33200, 2500)

        decision = FramingEngine().decide(
            config,
            _snapshot(VideoSourceKey.AUDIENCE),
            _snapshot(VideoSourceKey.PTZ, subject),
            _locked(),
            (),
            geometry,
            current_pose=current,
        )

        self.assertEqual(FramingState.READY, decision.state)
        self.assertLess(geometry.relative_footprints[-1], 0.75)
        self.assertGreater(decision.desired_pose.zoom, current.zoom)

    def test_off_center_subject_is_centered_before_zooming_in(self) -> None:
        subject = TrackedSubject(
            1,
            NormalizedRect(0.70, 0.20, 0.12, 0.56),
            0.92,
            False,
            5,
            time.monotonic(),
        )
        geometry = _Geometry()
        config = CameraTrackingConfig(enabled=True).automation
        config.mode = "subject"
        current = PtzMotorPose(43150, 33200, 2500)

        decision = FramingEngine().decide(
            config,
            _snapshot(VideoSourceKey.AUDIENCE),
            _snapshot(VideoSourceKey.PTZ, subject),
            _locked(),
            (),
            geometry,
            current_pose=current,
        )

        self.assertEqual(FramingState.READY, decision.state)
        self.assertEqual(1.0, geometry.relative_footprints[-1])
        self.assertEqual(current.zoom, decision.desired_pose.zoom)

    def test_clear_off_podium_subject_does_not_run_away_to_stage_wide(self) -> None:
        target, desired = _adaptive_ptz_composition(
            CameraTrackingConfig(enabled=True).automation,
            [NormalizedRect(0.45, 0.12, 0.10, 0.36)],
            podium=False,
        )
        relative_footprint = max(
            target.height / desired,
            target.width / (desired * 16.0 / 9.0),
        )

        self.assertLess(relative_footprint, 1.0)
        self.assertGreater(target.height, 0.50)

    def test_centered_live_subject_holds_current_pan(self) -> None:
        subject = TrackedSubject(
            1,
            NormalizedRect(0.45, 0.08, 0.10, 0.58),
            0.91,
            False,
            5,
            time.monotonic(),
        )
        geometry = _Geometry()
        config = CameraTrackingConfig(enabled=True).automation
        config.mode = "subject"
        current = PtzMotorPose(43700, 33900, 3200)

        decision = FramingEngine().decide(
            config,
            _snapshot(VideoSourceKey.AUDIENCE),
            _snapshot(VideoSourceKey.PTZ, subject),
            _locked(),
            (_stage(),),
            geometry,
            current_pose=current,
        )

        self.assertEqual(current.pan, decision.desired_pose.pan)

    def test_podium_framing_uses_guarded_center_of_region(self) -> None:
        subject = TrackedSubject(
            1,
            NormalizedRect(0.82, 0.10, 0.08, 0.18),
            0.93,
            False,
            5,
            time.monotonic(),
        )
        geometry = _Geometry()
        config = CameraTrackingConfig(enabled=True).automation
        config.mode = "subject"

        decision = FramingEngine().decide(
            config,
            _snapshot(VideoSourceKey.AUDIENCE, subject),
            _snapshot(VideoSourceKey.PTZ),
            _locked(),
            (_stage(), _stage("podium", name="Podium")),
            geometry,
        )

        self.assertFalse(decision.podium_framing)

    def test_target_identity_handoff_preserves_dwell_for_same_location(self) -> None:
        previous = NormalizedRect(0.40, 0.10, 0.15, 0.50)
        nearby = NormalizedRect(0.46, 0.10, 0.15, 0.50)

        self.assertTrue(_target_is_continuous(previous, nearby, 1, 1))
        self.assertFalse(_target_is_continuous(previous, nearby, 1, 2))

    def test_audience_detector_suppresses_people_outside_live_stage_regions(self) -> None:
        broker = LatestFrameBroker()
        config = CameraTrackingConfig(
            enabled=True,
            analyze_audience=True,
            analyze_ptz=False,
            analysis_fps=12,
        )
        service = PersonTrackingService(
            broker,
            config,
            detector_factory=lambda _config: _Detector(),
            region_provider=lambda: (_stage(),),
        )
        service.start()
        try:
            packet = broker.publish(VideoSourceKey.AUDIENCE, _image(), frame_rate=30)
            deadline = time.monotonic() + 2
            snapshot = service.snapshot(VideoSourceKey.AUDIENCE)
            while snapshot.analyzed_sequence < packet.sequence and time.monotonic() < deadline:
                time.sleep(0.01)
                snapshot = service.snapshot(VideoSourceKey.AUDIENCE)
            self.assertEqual(2, snapshot.raw_candidates)
            self.assertEqual(1, snapshot.suppressed_candidates)
            self.assertEqual(1, len(snapshot.subjects))
            self.assertEqual(("Stage",), snapshot.active_region_names)
        finally:
            service.stop()

    def test_framing_modes_group_stage_and_apply_podium_policy(self) -> None:
        subject = TrackedSubject(
            1,
            NormalizedRect(0.40, 0.10, 0.10, 0.18),
            0.93,
            True,
            5,
            time.monotonic(),
        )
        audience = _snapshot(VideoSourceKey.AUDIENCE, subject)
        ptz = _snapshot(VideoSourceKey.PTZ)
        geometry = _Geometry()
        config = CameraTrackingConfig(enabled=True).automation
        config.mode = "subject"
        decision = FramingEngine().decide(
            config,
            audience,
            ptz,
            _locked(),
            (_stage(), _stage("podium", name="Podium")),
            geometry,
        )
        self.assertEqual(FramingState.READY, decision.state)
        self.assertTrue(decision.podium_framing)
        self.assertEqual(config.podium_subject_frame_height, geometry.frame_heights[-1])

        config.mode = "stage"
        decision = FramingEngine().decide(
            config,
            audience,
            ptz,
            _locked(),
            (_stage(),),
            geometry,
        )
        self.assertEqual(FramingState.READY, decision.state)
        self.assertFalse(decision.podium_framing)
        self.assertGreater(decision.target_bounds.height, subject.bounds.height)

    def test_stage_mode_follows_ptz_subject_when_wide_view_cannot_detect_it(self) -> None:
        subject = TrackedSubject(
            8,
            NormalizedRect(0.42, 0.08, 0.16, 0.30),
            0.89,
            False,
            5,
            time.monotonic(),
        )
        audience = _snapshot(VideoSourceKey.AUDIENCE)
        ptz = _snapshot(VideoSourceKey.PTZ, subject)
        geometry = _Geometry()
        config = CameraTrackingConfig(enabled=True).automation
        config.mode = "stage"

        decision = FramingEngine().decide(
            config,
            audience,
            ptz,
            _locked(),
            (_stage(),),
            geometry,
            current_pose=PtzMotorPose(43700, 33900, 2000),
        )

        self.assertEqual(FramingState.READY, decision.state)
        self.assertEqual(("ptz:8",), decision.target_ids)
        self.assertEqual(0.89, decision.target_confidence)

    def test_subject_mode_automatically_adopts_unselected_ptz_subjects(self) -> None:
        subject = TrackedSubject(
            12,
            NormalizedRect(0.35, 0.10, 0.22, 0.48),
            0.91,
            False,
            6,
            time.monotonic(),
        )
        geometry = _Geometry()
        config = CameraTrackingConfig(enabled=True).automation
        config.mode = "subject"

        decision = FramingEngine().decide(
            config,
            _snapshot(VideoSourceKey.AUDIENCE),
            _snapshot(VideoSourceKey.PTZ, subject),
            _locked(),
            (),
            geometry,
            current_pose=PtzMotorPose(43700, 33900, 2000),
        )

        self.assertEqual(FramingState.READY, decision.state)
        self.assertEqual(("ptz:12",), decision.target_ids)

    def test_locked_group_combines_ptz_and_audience_reacquisition(self) -> None:
        now = time.monotonic()
        ptz_subject = TrackedSubject(
            1,
            NormalizedRect(0.20, 0.10, 0.14, 0.50),
            0.92,
            True,
            6,
            now,
        )
        audience_subject = TrackedSubject(
            2,
            NormalizedRect(0.70, 0.12, 0.12, 0.48),
            0.90,
            True,
            6,
            now,
        )
        config = CameraTrackingConfig(enabled=True).automation
        config.mode = "subject"

        decision = FramingEngine().decide(
            config,
            _snapshot(VideoSourceKey.AUDIENCE, audience_subject),
            _snapshot(VideoSourceKey.PTZ, ptz_subject),
            _locked(),
            (),
            _Geometry(),
            current_pose=PtzMotorPose(43700, 33900, 2000),
            subject_targets_locked=True,
        )

        self.assertEqual(FramingState.READY, decision.state)
        self.assertEqual(2, len(decision.target_ids))
        self.assertLess(decision.target_bounds.x, 0.25)
        self.assertGreater(decision.target_bounds.x + decision.target_bounds.width, 0.75)

    def test_click_to_frame_point_preserves_zoom_and_box_changes_zoom(self) -> None:
        video = _Video(
            _snapshot(VideoSourceKey.AUDIENCE),
            _snapshot(VideoSourceKey.PTZ),
        )
        panasonic = _Panasonic()
        config = CameraTrackingConfig(enabled=True, scene_regions=[_stage()])
        geometry = _Geometry()
        service = PtzAutomationService(
            video,
            panasonic,
            config,
            Path("/tmp"),
            geometry_loader=lambda _root: geometry,
        )

        point_pose = service.frame_live_target(0.45, 0.25)
        self.assertEqual(2000, point_pose.zoom)
        self.assertEqual([], panasonic.zoom_commands)

        box_pose = service.frame_live_target(0.30, 0.10, 0.25, 0.35)
        self.assertEqual(3000, box_pose.zoom)
        self.assertEqual([3000], panasonic.zoom_commands)

    def test_click_to_frame_reference_does_not_require_live_audience_lock(self) -> None:
        video = _Video(
            _snapshot(VideoSourceKey.AUDIENCE),
            _snapshot(VideoSourceKey.PTZ),
        )
        video.relocalization.value.state = RelocalizationState.LOCKING
        panasonic = _Panasonic()
        service = PtzAutomationService(
            video,
            panasonic,
            CameraTrackingConfig(enabled=True, scene_regions=[_stage()]),
            Path("/tmp"),
            geometry_loader=lambda _root: _Geometry(),
        )

        pose = service.frame_reference_target(-0.05, 0.30)

        self.assertEqual((pose.pan, pose.tilt), panasonic.pan_tilt_commands[-1])

    def test_click_to_frame_panorama_uses_ptz_native_geometry(self) -> None:
        video = _Video(
            _snapshot(VideoSourceKey.AUDIENCE),
            _snapshot(VideoSourceKey.PTZ),
        )
        panasonic = _Panasonic()
        panorama_geometry = _Geometry()
        service = PtzAutomationService(
            video,
            panasonic,
            CameraTrackingConfig(enabled=True, scene_regions=[_stage()]),
            Path("/tmp"),
            geometry_loader=lambda _root: self.fail(
                "Audience geometry must not serve a panorama click"
            ),
            panorama_geometry_loader=lambda _root: panorama_geometry,
        )

        pose = service.frame_panorama_target(-0.20, 0.35)

        self.assertEqual((pose.pan, pose.tilt), panasonic.pan_tilt_commands[-1])
        self.assertEqual([0.70], panorama_geometry.frame_heights)

    def test_geometry_extrapolates_within_calibrated_image_footprints(self) -> None:
        def transform_for(center_x: float, center_y: float):
            return (
                (0.5, 0.0, (center_x - 0.25) * 1000.0),
                (0.0, 0.5, (center_y - 0.25) * 500.0),
                (0.0, 0.0, 1.0),
            )

        anchors = tuple(
            CalibrationPoseAnchor(
                name=f"{x}-{y}",
                center_x=x,
                center_y=y,
                footprint_width=0.50,
                footprint_height=0.50,
                motor=PtzMotorPose(
                    round(42000 + x * 5000),
                    round(33000 + y * 4000),
                    2000,
                ),
                ptz_size=(1000, 500),
                audience_size=(1000, 500),
                ptz_to_audience=transform_for(x, y),
            )
            for x, y in ((0.30, 0.30), (0.70, 0.30), (0.30, 0.70), (0.70, 0.70))
        )
        geometry = PtzGeometryModel(Path("/approved/map.json"), "now", anchors)

        pose = geometry.pose_for_target(
            NormalizedRect(0.88, 0.40, 0.04, 0.08),
            desired_frame_height=0.70,
        )

        self.assertTrue(geometry.contains_reference_point(0.90, 0.44))
        self.assertGreater(pose.pan, max(item.motor.pan for item in anchors))
        current_view = geometry.reference_polygon_for_pose(anchors[-1].motor)
        self.assertEqual(4, len(current_view))
        self.assertAlmostEqual(0.45, min(x for x, _y in current_view), places=5)
        self.assertAlmostEqual(0.95, max(x for x, _y in current_view), places=5)

        between_anchors = PtzMotorPose(44500, 35000, 2000)
        projected = geometry.ptz_rect_to_reference(
            NormalizedRect(0.45, 0.45, 0.10, 0.10),
            between_anchors,
        )
        self.assertAlmostEqual(0.50, projected.center[0], places=4)
        self.assertAlmostEqual(0.50, projected.center[1], places=4)

    def test_guarded_controller_sends_only_bounded_absolute_steps_after_arm(self) -> None:
        subject = TrackedSubject(
            1,
            NormalizedRect(0.42, 0.10, 0.08, 0.15),
            0.94,
            False,
            5,
            time.monotonic(),
        )
        audience = _snapshot(VideoSourceKey.AUDIENCE, subject)
        video = _Video(audience, _snapshot(VideoSourceKey.PTZ))
        panasonic = _Panasonic()
        config = CameraTrackingConfig(
            enabled=True,
            scene_regions=[_stage()],
        )
        config.automation.mode = "stage"
        config.automation.target_dwell_seconds = 0.0
        config.automation.minimum_command_interval_seconds = 0.25
        geometry = _Geometry()
        service = PtzAutomationService(
            video,
            panasonic,
            config,
            Path("/tmp"),
            geometry_loader=lambda _root: geometry,
        )
        service.start()
        try:
            service.set_shadow_active(True)
            time.sleep(0.35)
            self.assertEqual([], panasonic.pan_tilt_commands)
            self.assertEqual([], panasonic.zoom_commands)

            ok, _message = service.arm()
            self.assertTrue(ok)
            deadline = time.monotonic() + 2
            while not panasonic.pan_tilt_commands and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(panasonic.pan_tilt_commands)
            self.assertLessEqual(
                abs(panasonic.pan_tilt_commands[0][0] - 43700),
                config.automation.maximum_pan_step_units,
            )
            self.assertLessEqual(
                abs(panasonic.pan_tilt_commands[0][1] - 33900),
                config.automation.maximum_tilt_step_units,
            )
            self.assertLessEqual(
                abs(panasonic.zoom_commands[0] - 2000),
                config.automation.maximum_zoom_step_units,
            )
            service.manual_override("test operator control")
            self.assertFalse(service.snapshot().armed)
            self.assertFalse(service.snapshot().motion_authority)
        finally:
            service.shutdown()

    def test_camera_settling_is_not_mistaken_for_manual_override(self) -> None:
        audience = _snapshot(VideoSourceKey.AUDIENCE)
        video = _Video(audience, _snapshot(VideoSourceKey.PTZ))
        panasonic = _Panasonic()
        config = CameraTrackingConfig(enabled=True, scene_regions=[_stage()])
        service = PtzAutomationService(video, panasonic, config, Path("/tmp"))
        now = time.monotonic()
        service._last_command = now
        service._snapshot.commanded_pose = PtzMotorPose(44000, 34000, 3000)
        far_position = PtzMotorPose(43000, 33000, 2500)

        self.assertFalse(service._manual_motion_detected(far_position, now + 5.5))
        self.assertTrue(
            service._manual_motion_detected(
                far_position,
                now + 6.1,
            )
        )

    def test_subject_detector_loss_holds_without_turning_tracking_off(self) -> None:
        video = _Video(
            _snapshot(VideoSourceKey.AUDIENCE),
            _snapshot(VideoSourceKey.PTZ),
        )
        config = CameraTrackingConfig(enabled=True, scene_regions=[_stage()])
        config.automation.mode = "subject"
        config.automation.target_loss_disarm_seconds = 1.0
        service = PtzAutomationService(video, _Panasonic(), config, Path("/tmp"))
        service._armed = True
        service._snapshot.armed = True
        service._snapshot.motion_authority = True
        service._last_target_seen = 10.0

        service._handle_target_loss(30.0, "detector dropout")

        self.assertTrue(service.snapshot().armed)
        self.assertEqual(AutomationState.ARMED_WAITING, service.snapshot().state)
        self.assertIn("Tracking remains on", service.snapshot().message)

    def test_async_visca_override_neutralizes_latched_velocity(self) -> None:
        async def exercise() -> None:
            video = _Video(
                _snapshot(VideoSourceKey.AUDIENCE),
                _snapshot(VideoSourceKey.PTZ),
            )
            panasonic = _Panasonic()
            config = CameraTrackingConfig(enabled=True, scene_regions=[_stage()])
            config.automation.mode = "subject"
            service = PtzAutomationService(video, panasonic, config, Path("/tmp"))
            service._armed = True
            service._snapshot.armed = True
            service._snapshot.motion_authority = True
            service._last_velocity_command = PtzVelocityCommand(24, 36, 42)

            await service.manual_override_async("VISCA bridge command")

            self.assertFalse(service.snapshot().armed)
            self.assertEqual([(50, 50)], panasonic.pan_tilt_speed_commands)
            self.assertEqual([50], panasonic.zoom_speed_commands)
            self.assertEqual(set(), service._pending_stop_tasks)

        asyncio.run(exercise())

    def test_next_absolute_step_waits_for_panasonic_to_settle(self) -> None:
        audience = _snapshot(VideoSourceKey.AUDIENCE)
        video = _Video(audience, _snapshot(VideoSourceKey.PTZ))
        panasonic = _Panasonic()
        config = CameraTrackingConfig(enabled=True, scene_regions=[_stage()])
        service = PtzAutomationService(video, panasonic, config, Path("/tmp"))
        now = time.monotonic()
        service._last_command = now
        service._snapshot.commanded_pose = PtzMotorPose(44000, 34000, 3000)

        service._send_bounded_command(
            PtzMotorPose(43700, 33900, 2800),
            PtzMotorPose(44500, 34500, 3400),
            now + 1.0,
        )

        self.assertEqual([], panasonic.pan_tilt_commands)
        self.assertEqual([], panasonic.zoom_commands)

    def test_subject_tracking_uses_velocity_without_claiming_absolute_pose(self) -> None:
        subject = TrackedSubject(
            1,
            NormalizedRect(0.66, 0.08, 0.16, 0.40),
            0.94,
            False,
            5,
            time.monotonic(),
        )
        video = _Video(
            _snapshot(VideoSourceKey.AUDIENCE),
            _snapshot(VideoSourceKey.PTZ, subject),
        )
        panasonic = _Panasonic()
        config = CameraTrackingConfig(enabled=True, scene_regions=[_stage()])
        config.automation.mode = "subject"
        config.automation.target_dwell_seconds = 0.0
        geometry = _Geometry()
        service = PtzAutomationService(
            video,
            panasonic,
            config,
            Path("/tmp"),
            geometry_loader=lambda _root: geometry,
        )
        service._armed = True
        service._geometry = geometry
        service._snapshot.armed = True
        service._snapshot.motion_authority = True
        service._snapshot.commanded_pose = None
        service._last_command = time.monotonic()

        service._cycle(service._last_command + 0.7)

        snapshot = service.snapshot()
        self.assertIsNone(snapshot.commanded_pose)
        self.assertIsNotNone(snapshot.decision.desired_pose)
        self.assertEqual([], panasonic.pan_tilt_commands)
        self.assertTrue(panasonic.pan_tilt_speed_commands)

    def test_in_flight_decision_cannot_overwrite_disarmed_state(self) -> None:
        audience = _snapshot(VideoSourceKey.AUDIENCE)
        video = _Video(audience, _snapshot(VideoSourceKey.PTZ))
        panasonic = _Panasonic()
        config = CameraTrackingConfig(enabled=True, scene_regions=[_stage()])
        config.automation.mode = "subject"
        geometry = _Geometry()
        service = PtzAutomationService(
            video,
            panasonic,
            config,
            Path("/tmp"),
            geometry_loader=lambda _root: geometry,
        )
        service._armed = True
        service._geometry = geometry
        service._snapshot.armed = True
        service._snapshot.motion_authority = True

        class _DisarmingFraming:
            def decide(self, *_args, **_kwargs):
                service.disarm("Tenveo Auto Focus button turned tracking off")
                return FramingDecision(
                    FramingState.WAITING,
                    "subject",
                    "No stable subject is visible",
                )

        service.framing = _DisarmingFraming()
        service._cycle(time.monotonic())

        snapshot = service.snapshot()
        self.assertFalse(snapshot.armed)
        self.assertEqual("disarmed", snapshot.state)
        self.assertEqual(
            "Tenveo Auto Focus button turned tracking off",
            snapshot.message,
        )
        self.assertEqual([], panasonic.pan_tilt_commands)

    def test_automation_api_is_readable_but_remote_controls_require_token(self) -> None:
        with TemporaryDirectory() as directory:
            context = build_context(Path(directory))
            context.config.api.lan_access_enabled = True
            context.config.api.require_token_for_privileged = True
            context.config.api.access_token = "phase-five-secret"
            client = TestClient(create_app(context))
            self.assertEqual(200, client.get("/api/camera/automation").status_code)
            self.assertEqual(
                401,
                client.post("/api/camera/automation/disarm").status_code,
            )
            response = client.post(
                "/api/camera/automation/disarm",
                headers={"X-Production-Hub-Token": "phase-five-secret"},
            )
            self.assertEqual(200, response.status_code)
            self.assertFalse(response.json()["motionAuthority"])
            context.video.shutdown()


if __name__ == "__main__":
    unittest.main()
