from dataclasses import replace

import pytest

from production_hub.tracking.association import SubjectAssociator
from production_hub.tracking.frame_alignment import PtzFrameAlignment
from production_hub.tracking.models import NormalizedRect, PersonCandidate
from production_hub.tracking.ptz_geometry import PtzMotorPose
from production_hub.video.models import VideoSourceKey
from testing.harness.ptz_tracking import build_geometry, _detector_snapshot


def candidate(x=.4):
    return PersonCandidate(NormalizedRect(x, .2, .15, .5), .9)


def test_occluded_identity_returns_without_drawing_or_selecting_stale_subject():
    tracker = SubjectAssociator(maximum_missed_frames=1)
    original = tracker.update([candidate()], observed_monotonic=10)[0].track_id
    tracker.toggle(original)
    tracker.update([], observed_monotonic=10.25)
    assert tracker.update([], observed_monotonic=10.5) == ()
    assert not tracker.selected_ids
    returned = tracker.update([candidate(.41)], observed_monotonic=11)[0]
    assert returned.track_id == original
    assert not returned.selected


def test_identity_memory_expires_and_does_not_reassign_distant_person():
    for elapsed, x in ((3, .4), (1, .7)):
        tracker = SubjectAssociator(maximum_missed_frames=0)
        original = tracker.update([candidate()], observed_monotonic=10)[0].track_id
        tracker.update([], observed_monotonic=10.25)
        returned = tracker.update([candidate(x)], observed_monotonic=10 + elapsed)[0]
        assert returned.track_id != original


def test_identity_memory_rejects_ambiguous_return_and_clears_on_reset():
    tracker = SubjectAssociator(maximum_missed_frames=0)
    originals = tracker.update([candidate(.39), candidate(.41)], observed_monotonic=10)
    tracker.update([], observed_monotonic=10.25)
    returned = tracker.update([candidate(.4)], observed_monotonic=10.5)[0]
    assert returned.track_id not in {p.track_id for p in originals}
    tracker.reset()
    assert not tracker._dormant


def snapshot(bounds, captured, completed):
    value = _detector_snapshot(VideoSourceKey.PTZ, [(1, bounds, .9, 4)], completed)
    return replace(value, frame_captured_monotonic=captured)


def test_prediction_compensates_delay_and_stops_without_velocity_tail():
    geometry = build_geometry()
    pose = PtzMotorPose(42400, 34964, 3450)
    aligner = PtzFrameAlignment()
    bounds = NormalizedRect(.4, .2, .15, .5)
    aligner.align(snapshot(bounds, 10, 10), geometry, pose, 10)
    aligner.align(snapshot(bounds, 10, 10.25), geometry, pose, 10.25)
    moved = replace(bounds, x=.45)
    predicted = aligner.align(snapshot(moved, 10.25, 10.5), geometry, pose, 10.5)
    assert predicted.subjects[0].bounds.x == pytest.approx(.47, abs=.002)
    moved_again = replace(bounds, x=.50)
    predicted = aligner.align(snapshot(moved_again, 10.5, 10.75), geometry, pose, 10.75)
    assert predicted.subjects[0].bounds.x == pytest.approx(.52, abs=.002)
    stopped = aligner.align(snapshot(moved_again, 10.75, 11), geometry, pose, 11)
    assert stopped.subjects[0].bounds.x == pytest.approx(.50, abs=.002)


def test_camera_motion_is_not_predicted_as_subject_motion():
    geometry = build_geometry()
    first = PtzMotorPose(42400, 34964, 3450)
    second = PtzMotorPose(42380, 34964, 3450)
    bounds = NormalizedRect(.4, .2, .15, .5)
    reference = geometry.ptz_rect_to_reference(bounds, first)
    moved_image = geometry.reference_rect_to_ptz_image(reference, second)
    aligner = PtzFrameAlignment()
    aligner.align(snapshot(bounds, 10, 10), geometry, first, 10)
    aligner.align(snapshot(bounds, 10, 10.25), geometry, second, 10.25)
    aligned = aligner.align(snapshot(moved_image, 10.25, 10.5), geometry, second, 10.5)
    assert aligned.subjects[0].bounds.x == pytest.approx(moved_image.x, abs=.002)


def test_partial_box_before_occlusion_keeps_identity_when_full_body_returns():
    tracker = SubjectAssociator(maximum_missed_frames=0)
    partial = PersonCandidate(NormalizedRect(.36, .506, .096, .191), .65)
    full = PersonCandidate(NormalizedRect(.337, .512, .140, .279), .5)
    original = tracker.update([partial], observed_monotonic=10)[0].track_id
    tracker.update([], observed_monotonic=10.25)
    assert tracker.update([full], observed_monotonic=11.5)[0].track_id == original


def test_prediction_does_not_extrapolate_a_sudden_box_jump():
    geometry = build_geometry()
    pose = PtzMotorPose(42400, 34964, 3450)
    bounds = NormalizedRect(.25, .2, .10, .5)
    aligner = PtzFrameAlignment()
    aligner.align(snapshot(bounds, 10, 10), geometry, pose, 10)
    aligner.align(snapshot(bounds, 10, 10.25), geometry, pose, 10.25)
    jumped = replace(bounds, x=.45)
    result = aligner.align(snapshot(jumped, 10.25, 10.5), geometry, pose, 10.5)
    assert result.subjects[0].bounds.x == pytest.approx(.45, abs=.002)


def test_prediction_does_not_pan_a_group_for_one_members_gesture():
    geometry = build_geometry()
    pose = PtzMotorPose(42400, 34964, 3450)
    left = NormalizedRect(.25, .2, .15, .5)
    right = NormalizedRect(.60, .2, .15, .5)
    def group(a, captured, completed):
        value = _detector_snapshot(VideoSourceKey.PTZ,
                                   [(1, a, .9, 4), (2, right, .9, 4)], completed)
        return replace(value, frame_captured_monotonic=captured)
    aligner = PtzFrameAlignment()
    aligner.align(group(left, 10, 10), geometry, pose, 10)
    aligner.align(group(left, 10, 10.25), geometry, pose, 10.25)
    moved = replace(left, x=.30)
    result = aligner.align(group(moved, 10.25, 10.5), geometry, pose, 10.5)
    assert result.subjects[0].bounds.x == pytest.approx(moved.x, abs=.002)
    assert result.subjects[1].bounds.x == pytest.approx(right.x, abs=.002)


def test_extreme_outward_motion_gets_recovery_authority_with_bounded_slew():
    from production_hub.core.config.models import PtzAutomationConfig
    from production_hub.tracking.velocity_controller import SmoothPtzVelocityController
    config = PtzAutomationConfig()
    pose = PtzMotorPose(42400, 34964, 3450)
    gentle = SmoothPtzVelocityController()
    responsive = SmoothPtzVelocityController()
    speeds = []
    ordinary = []
    for i in range(10):
        bounds = NormalizedRect(.85 + i * .007, .2, .1, .5)
        args = dict(actual=pose, desired=pose, config=config, now=10 + i * .125,
                    live_bounds=bounds, ease_motion=True)
        speeds.append(responsive.update(**args, responsive_tracking=True).pan)
        ordinary.append(gentle.update(**args, responsive_tracking=False).pan)
    assert max(speeds) > max(ordinary)
    assert max(abs(b-a) for a, b in zip(speeds, speeds[1:])) <= 8
    assert not responsive.halt().moving


@pytest.mark.parametrize('count,speed,motor_delay', [(1, 1.5, .15), (2, .5, 0), (3, 1.5, 0)])
@pytest.mark.parametrize('direction', [-1, 1])
def test_delayed_stage_walk_retains_people_and_settles(count, speed, motor_delay, direction):
    from testing.harness.stage_simulation import (
        DT, DelayedPanasonic, ObservationEffects, evaluate, make_case,
    )
    from testing.harness.ptz_tracking import run_scenario
    case, _ = make_case('prediction-stage-regression', count=count, speed=speed, direction=direction)
    case = replace(case, motion_easing_enabled=True, tracking_refinements_enabled=True)
    result = run_scenario(
        case, decision_interval=DT,
        camera_factory=lambda pose: DelayedPanasonic(
            pose, delay=motor_delay, response=.12 if motor_delay else 0),
        observation_transform=ObservationEffects(latency=.25),
    )
    assert evaluate(result, case)['passed']
