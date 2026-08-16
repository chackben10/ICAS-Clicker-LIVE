from __future__ import annotations

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
from production_hub.tracking.ptz_automation import PtzAutomationService
from production_hub.tracking.ptz_geometry import (
    CalibrationPoseAnchor,
    PtzGeometryModel,
    PtzMotorPose,
)
from production_hub.tracking.service import PersonTrackingService
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

    def pose_for_target(self, _target, *, desired_frame_height: float) -> PtzMotorPose:
        self.frame_heights.append(desired_frame_height)
        return PtzMotorPose(45000, 34500, 3000)

    def ptz_rect_to_reference(self, bounds, _pose):
        return bounds


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


def _snapshot(source: VideoSourceKey, *subjects: TrackedSubject) -> TrackingSnapshot:
    return TrackingSnapshot(
        source=source,
        state=TrackingState.RUNNING,
        subjects=tuple(subjects),
        last_analysis_monotonic=time.monotonic(),
    )


class PtzAutomationPhase45Tests(unittest.TestCase):
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
