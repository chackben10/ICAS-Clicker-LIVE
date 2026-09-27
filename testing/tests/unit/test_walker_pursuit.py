"""Keeping pace with a walker at sermon zoom (Dev experiments).

At zoom ~3500 the AW-HE120's speed-with-zoom feature makes the configured cap
(26) about 390 pan units/s; a person walking at 1 m/s needs ~620. These tests
pin the pieces of the fix: the measured motor model, the pace term (plus part
of the position correction, to close the gap), and the safeguards that keep it
from chasing a single jump, overshooting a stop, or acting at wide zoom.
"""

from __future__ import annotations

from production_hub.integrations.panasonic_awp import speed_model
from production_hub.tracking import velocity_controller as vc
from production_hub.tracking.ptz_automation import _WalkerVelocity
from production_hub.tracking.models import NormalizedRect, SubjectReferenceTarget


def test_speed_with_zoom_is_modelled_from_the_measured_camera() -> None:
    assert 2700 < speed_model.rate("pan", 26, 2238) < 2900
    assert 350 < speed_model.rate("pan", 26, 3500) < 420
    # A 1 m/s walker at sermon zoom needs more than the live cap of 26.
    assert speed_model.offset_for_rate("pan", 620, 3500) > 26
    assert speed_model.offset_for_rate("pan", 10_000, 3500, maximum=44) == 44


def test_pace_plus_part_of_the_correction_closes_the_gap_to_a_walker() -> None:
    # Pan units falling = view moving right = positive speed delta.
    alone = vc._with_walker_feedforward("pan", 0.0, -800.0, 3500)
    assert 30 < alone <= vc.PURSUIT_PAN_MAX
    behind = vc._with_walker_feedforward("pan", 6.0, -800.0, 3500)
    # Matching pace alone would hold the walker off-centre for the whole walk.
    assert behind == min(vc.PURSUIT_PAN_MAX, alone + vc.WALKER_CATCH_UP * 6.0)
    # A correction the other way (camera ahead) brakes instead.
    assert vc._with_walker_feedforward("pan", -6.0, -800.0, 3500) < alone


def test_pace_lets_go_when_the_camera_overtakes_the_walker() -> None:
    # The walker heads to +x; the subject sliding back to -x means they stopped.
    assert vc._with_walker_feedforward("pan", 0.0, -800.0, 3500, image_velocity=-0.3) == 0.0


def test_a_single_jump_is_not_a_walk() -> None:
    walk = vc._WalkEvidence()
    assert walk.observe(-500.0, 0.0) < 0.5
    # A jump's velocity estimate decays; a walker's does not.
    assert walk.observe(-300.0, 0.25) < 0.5


def test_a_sustained_walk_engages_fully() -> None:
    walk = vc._WalkEvidence()
    weights = [walk.observe(-600.0 - step * 20, step * 0.25, image_agrees=True) for step in range(3)]
    assert weights[-1] == 1.0


def test_tilt_directions_are_not_mirrored_like_pan() -> None:
    # Tilt units rising = view moving down = walker heading to +y in the image.
    assert vc._walk_image_direction("tilt", 300.0) == 1.0
    assert vc._walk_image_direction("pan", 300.0) == -1.0


def test_walker_velocity_coasts_through_a_dropout_instead_of_stopping() -> None:
    walker = _WalkerVelocity()
    bounds = NormalizedRect(0.4, 0.1, 0.05, 0.15)
    for step in range(4):
        target = SubjectReferenceTarget(1, bounds, 0.6, "ptz", observed_x=0.40 + 0.02 * step,
                                        observed_at=step * 0.25, observed_top=0.10)
        walker.update([target], step * 0.25)
    moving = walker.velocity[0]
    assert moving > 0.05
    coasted = walker.coast(1.0)
    assert 0.0 < coasted[0] < moving


def test_a_confirmed_wide_camera_sighting_is_followed_at_pursuit_speed() -> None:
    from types import SimpleNamespace

    from production_hub.tracking.ptz_geometry import PtzMotorPose

    config = SimpleNamespace(
        decision_fps=4.0, continuous_error_smoothing=0.58, continuous_velocity_smoothing=0.45,
        continuous_prediction_seconds=0.55, subject_center_deadband_x=0.06, subject_center_deadband_y=0.09,
        continuous_pan_max_speed=26, continuous_tilt_max_speed=14, continuous_zoom_max_speed=8,
        continuous_speed_slew_per_second=18.0, pan_deadband_units=80, tilt_deadband_units=60,
        zoom_deadband_units=35, continuous_zoom_dwell_seconds=0.6, group_zoom_out_max_speed=20,
    )
    actual = PtzMotorPose(43000, 33300, 3500)
    desired = PtzMotorPose(44500, 33300, 3500)  # 1500 pan units behind the walker
    speeds = {}
    for confirmed in (False, True):
        controller = vc.SmoothPtzVelocityController()
        for step in range(8):
            command = controller.update(
                actual=actual, desired=desired, config=config, now=step * 0.25, live_bounds=None,
                ease_motion=True, responsive_tracking=True, safe_zone_control=True,
                confirmed_wide_target=confirmed,
            )
        speeds[confirmed] = abs(command.pan - 50)
    assert speeds[False] <= 26 < speeds[True] <= vc.PURSUIT_PAN_MAX


def test_wide_camera_windows_are_fitted_to_the_drawn_stage() -> None:
    from types import SimpleNamespace

    from production_hub.tracking.service import _stage_detection_windows

    point = lambda x, y: SimpleNamespace(x=x, y=y)  # noqa: E731
    stage = SimpleNamespace(enabled=True, source="audience", kind="stage",
                            points=[point(0.31, 0.02), point(0.77, 0.02), point(0.77, 0.23), point(0.31, 0.23)])
    floor = SimpleNamespace(enabled=True, source="audience", kind="front_stage",
                            points=[point(0.19, 0.19), point(0.80, 0.19), point(0.80, 0.36), point(0.19, 0.36)])
    windows = _stage_detection_windows([stage, floor])
    assert len(windows) == 4
    assert windows[0].x <= 0.14 and windows[-1].x + windows[-1].width >= 0.85
    # Tall enough for someone standing on the floor in front, not into the pews.
    assert all(0.35 <= item.height <= 0.40 for item in windows)
    assert _stage_detection_windows([]) == ()


def _sermon_config():
    from types import SimpleNamespace

    return SimpleNamespace(
        decision_fps=4.0, continuous_error_smoothing=0.58, continuous_velocity_smoothing=0.45,
        continuous_prediction_seconds=0.55, subject_center_deadband_x=0.06, subject_center_deadband_y=0.09,
        continuous_pan_max_speed=26, continuous_tilt_max_speed=14, continuous_zoom_max_speed=8,
        continuous_speed_slew_per_second=18.0, pan_deadband_units=80, tilt_deadband_units=60,
        zoom_deadband_units=35, continuous_zoom_dwell_seconds=0.6, group_zoom_out_max_speed=20,
    )


def test_a_brief_detection_gap_eases_the_pan_down_instead_of_stopping_dead() -> None:
    controller = vc.SmoothPtzVelocityController()
    controller._pan_delta, controller._zoom_delta, controller._last_update = 36.0, -10.0, 0.0
    first = controller.coast_through_loss(0.25)
    assert 50 + 10 < first.pan < 50 + 36 and first.zoom == 50
    later = controller.coast_through_loss(0.75)
    assert later.pan < first.pan


def test_a_head_on_the_top_edge_tilts_up_even_with_the_box_centre_in_the_dead_zone() -> None:
    from production_hub.tracking.ptz_geometry import PtzMotorPose

    pose = PtzMotorPose(43000, 33700, 3300)
    speeds = {}
    for top in (0.20, 0.01):
        controller = vc.SmoothPtzVelocityController()
        # Tall box, bottom clipped by the frame: its centre is only just above the dead zone.
        bounds = NormalizedRect(0.45, top, 0.10, 0.66)
        for step in range(4):
            command = controller.update(
                actual=pose, desired=pose, config=_sermon_config(), now=step * 0.25, live_bounds=bounds,
                ease_motion=True, responsive_tracking=True, safe_zone_control=True,
            )
        speeds[top] = command.tilt - 50  # above 50 tilts up
    assert speeds[0.01] > 8 and speeds[0.01] > 3 * max(1, speeds[0.20])


def test_off_shot_widening_needs_an_escape_not_a_dropout() -> None:
    from production_hub.tracking.ptz_geometry import PtzMotorPose

    actual = PtzMotorPose(43000, 33300, 3500)
    controller = vc.SmoothPtzVelocityController()
    # Lost mid-frame while standing (a bend): hold the zoom.
    assert controller._escape_widening(actual, None, True, PtzMotorPose(43100, 33300, 3500)) == 0.0
    # The wide camera has them well outside this shot: breathe out.
    assert controller._escape_widening(actual, None, True, PtzMotorPose(44000, 33300, 3500)) > 0.0
    # Only for a confirmed wide-camera sighting.
    assert controller._escape_widening(actual, None, False, PtzMotorPose(44000, 33300, 3500)) == 0.0
