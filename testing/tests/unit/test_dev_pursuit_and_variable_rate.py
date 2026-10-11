"""Dev PTZ pursuit fixes from the 2026-10-11 diagnostic recordings.

Each test names the live failure it guards. See PTZ_TRACKING_HANDOFF.md.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from production_hub.core.config.models import CameraTrackingConfig, PtzAutomationConfig
from production_hub.tracking.models import NormalizedRect, TrackedSubject, TrackingSnapshot, TrackingState
from production_hub.tracking.ptz_automation import PtzAutomationService, _centred_motor_pose
from production_hub.tracking.ptz_geometry import PtzMotorPose
from production_hub.tracking.velocity_controller import SmoothPtzVelocityController
from production_hub.video.models import VideoSourceKey
from testing.harness.ptz_tracking import (
    SimulatedPanasonic, SimulatedVideo, build_geometry, person, pose_framing, run_scenario, scenario,
)


def _service(geometry=None):
    geometry = geometry or build_geometry()
    service = PtzAutomationService(
        SimulatedVideo(), SimulatedPanasonic(PtzMotorPose(42400, 34964, 3450)),
        CameraTrackingConfig(enabled=True), Path("/tmp/production-hub-harness"),
        geometry_loader=lambda _root: geometry,
    )
    service.tracking_refinements_enabled = True
    return service


def _snapshot(bounds: NormalizedRect) -> TrackingSnapshot:
    return TrackingSnapshot(
        source=VideoSourceKey.PTZ, state=TrackingState.RUNNING,
        subjects=(TrackedSubject(track_id=1, bounds=bounds, confidence=.9, selected=False, age_frames=5, last_seen_monotonic=0.0),),
    )


def test_one_person_walking_off_never_whips_toward_the_one_who_stayed():
    # Live run E (21:10:57): with one of a pair out of shot, Dev steered on
    # whoever was visible and swung 3,000 units against its own target.
    dt = 1 / 12
    a, b = person('a', x=.38), person('b', x=.44)
    frames = [[a, b]] * 30 + [[a, b.moved(x=.44 + .005 * j)] for j in range(60)]
    frames += [frames[-1]] * 72
    recording = run_scenario(
        scenario('walk-off', frames, initial_pose=pose_framing(frames[0], occupancy=.78),
                 motion_easing_enabled=True, tracking_refinements_enabled=True),
        decision_interval=dt,
        camera_factory=lambda pose: SimulatedPanasonic(pose, measured_rates=True),
    )
    speeds = [step.pan_speed for step in recording.steps]
    # The walker heads right; the camera may follow or hold, never swing left.
    assert all(speed >= 49 for speed in speeds)
    assert speeds[-1] == 50


def test_handoff_anchor_is_the_pose_that_centres_the_subject():
    # Live run D (21:05:48): the anchor was the raw pose with the speaker at
    # the frame edge, so recovery stopped half a frame short.
    geometry = build_geometry()
    actual = PtzMotorPose(42400, 34964, 3450)
    pan_span, tilt_span = geometry.image_axis_motor_units(actual)
    centred = _centred_motor_pose(actual, _snapshot(NormalizedRect(.00, .10, .10, .76)), geometry)
    # Subject at x=0.05: aiming left raises pan by ~0.45 of a frame.
    assert centred.pan == pytest.approx(actual.pan + .45 * pan_span, abs=2)
    assert centred.tilt == pytest.approx(actual.tilt + (.48 - .48) * tilt_span, abs=2)
    assert _centred_motor_pose(actual, TrackingSnapshot(source=VideoSourceKey.PTZ), geometry) == actual


def test_fresh_audience_match_follows_a_reversal_but_a_stale_one_does_not():
    # Live run D: the speaker walked off one way and came back; the guard
    # rejected every Audience reversal and searched the wrong way.
    service = _service()
    actual = PtzMotorPose(42400, 34964, 3450)
    service._prepare_handoff_guard(actual)
    service._handoff_pan_direction = 1  # last verified motion: pan rising
    reversed_target = PtzMotorPose(41600, 34964, 3450)
    stale, _reason = service._guard_audience_handoff(reversed_target, actual=actual, now=10.0)
    assert stale is None or stale.pan >= actual.pan
    fresh, reason = service._guard_audience_handoff(
        reversed_target, actual=actual, now=10.1, audience_fresh=True,
    )
    assert fresh is not None and fresh.pan == reversed_target.pan and reason == ""
    # A fresh match also restarts the safety clock.
    later, _ = service._guard_audience_handoff(
        reversed_target, actual=actual, now=10.1 + 30, audience_fresh=True,
    )
    assert later is not None


def test_detection_jump_is_treated_as_redetection_not_speed():
    # Boxes that teleport (identity glitches) must not fling the camera.
    controller = SmoothPtzVelocityController()
    config = PtzAutomationConfig()
    command = None
    for i, x in enumerate([.5] * 6 + [.9]):
        command = controller.update(
            actual=PtzMotorPose(43000, 34000, 3450), desired=PtzMotorPose(43000, 34000, 3450),
            config=config, now=i / 12, live_bounds=NormalizedRect(x - .06, .1, .12, .7),
            responsive_tracking=True, observed_at=i / 12,
        )
    assert abs(controller._world_vx) < .05
    assert command.pan > 50  # still centres the subject, by position only


def test_rising_head_gets_headroom_even_inside_the_centring_band():
    # Stage duck/rise: the head ended at the top edge while the box centre
    # sat inside the tilt hold band.
    controller = SmoothPtzVelocityController()
    command = None
    for i in range(6):
        command = controller.update(
            actual=PtzMotorPose(43000, 34000, 3450), desired=PtzMotorPose(43000, 34000, 3450),
            config=PtzAutomationConfig(), now=i / 12,
            live_bounds=NormalizedRect(.44, .01, .12, .90), responsive_tracking=True,
            observed_at=i / 12,
        )
    assert command.tilt > 50  # Panasonic tilt above 50 aims up


def test_tracking_service_rate_override_and_recent_rate_readout():
    from production_hub.tracking.service import PersonTrackingService
    from collections import deque

    service = PersonTrackingService.__new__(PersonTrackingService)
    service.config = CameraTrackingConfig(enabled=True)
    service._rate_overrides = {VideoSourceKey.PTZ: None, VideoSourceKey.AUDIENCE: None}
    assert service.analysis_rate(VideoSourceKey.PTZ) == service.config.analysis_fps
    service.set_analysis_rate(VideoSourceKey.PTZ, 30)
    assert service.analysis_rate(VideoSourceKey.PTZ) == 12.0
    service.set_analysis_rate(VideoSourceKey.PTZ, None)
    assert service.analysis_rate(VideoSourceKey.PTZ) == service.config.analysis_fps
    # Old throttled entries no longer drag the readout down after arming.
    times = deque([0.0, 1.0] + [10 + i / 12 for i in range(36)], maxlen=60)
    assert PersonTrackingService._calculate_rate(times) == pytest.approx(12, rel=.02)


def test_variable_rate_is_fast_while_moving_and_returns_to_config_when_calm():
    service = _service()
    requested = []
    service.video.tracking.set_analysis_rate = lambda source, fps: requested.append((source, fps))
    service._armed = True
    service._update_analysis_rate(100.0, True)
    assert requested[-1] == (VideoSourceKey.PTZ, 12.0)
    service._update_analysis_rate(101.0, False)
    assert requested[-1] == (VideoSourceKey.PTZ, 12.0)  # held through brief calm
    service._update_analysis_rate(102.0, False)
    assert requested[-1] == (VideoSourceKey.PTZ, service.config.analysis_fps)
    service.disarm("test")
    assert requested[-1] == (VideoSourceKey.PTZ, None)


def test_release_path_never_requests_a_rate_change():
    service = _service()
    service.tracking_refinements_enabled = False
    requested = []
    service.video.tracking.set_analysis_rate = lambda source, fps: requested.append(fps)
    service._armed = True
    service._update_analysis_rate(100.0, True)
    assert requested == []
