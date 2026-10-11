from production_hub.tracking.apple_vision import merge_pose_envelopes
from production_hub.tracking.models import NormalizedRect, PersonCandidate


def test_pose_assignment_keeps_occluded_neighbour_as_an_independent_person():
    # May skit: middle actor has a pose but no rectangle. The previous merger
    # attached both left and middle poses to the left actor's rectangle.
    rectangles = [PersonCandidate(NormalizedRect(.129,.368,.130,.282),.74),
                  PersonCandidate(NormalizedRect(.756,.612,.111,.235),.67)]
    poses = [NormalizedRect(.092,.408,.164,.463),
             NormalizedRect(.340,.525,.157,.260),
             NormalizedRect(.725,.651,.129,.259)]
    result = merge_pose_envelopes(rectangles,poses)
    assert len(result)==3
    assert all(person.bounds.width<.20 for person in result)


def test_pose_assignment_is_independent_of_detector_enumeration_order():
    rectangles = [PersonCandidate(NormalizedRect(.319,.107,.304,.682),.70),
                  PersonCandidate(NormalizedRect(.600,.006,.304,.795),.66)]
    poses = [NormalizedRect(.171,0,.302,.624),
             NormalizedRect(.560,0,.382,.955),
             NormalizedRect(.315,.228,.351,.551)]
    expected = merge_pose_envelopes(rectangles,poses)
    assert len(expected)==3
    assert merge_pose_envelopes(rectangles,list(reversed(poses)))==expected
    assert merge_pose_envelopes(list(reversed(rectangles)),poses)==expected


def test_delayed_box_is_reprojected_from_capture_pose_without_mutating_snapshot():
    from dataclasses import replace
    import pytest
    from production_hub.tracking.frame_alignment import PtzFrameAlignment
    from production_hub.tracking.ptz_geometry import PtzMotorPose
    from production_hub.video.models import VideoSourceKey
    from testing.harness.ptz_tracking import build_geometry, _detector_snapshot
    geometry=build_geometry()
    old=PtzMotorPose(42400,34964,3450)
    current=PtzMotorPose(42300,34964,3450)
    bounds=NormalizedRect(.40,.20,.20,.55)
    first=_detector_snapshot(VideoSourceKey.PTZ,[(1,bounds,.9,4)],100)
    aligner=PtzFrameAlignment()
    aligner.align(first,geometry,old,100)
    # Inference completes at 100.25 but the frame was captured at 100.0.
    delayed=replace(first,last_analysis_monotonic=100.25,frame_captured_monotonic=100,
                    subjects=tuple(replace(p,last_seen_monotonic=100.25) for p in first.subjects))
    result=aligner.align(delayed,geometry,current,100.25)
    expected=geometry.reference_rect_to_ptz_image(geometry.ptz_rect_to_reference(bounds,old),current)
    assert result.subjects[0].bounds.x==pytest.approx(expected.x)
    assert result.subjects[0].bounds.x<bounds.x
    assert delayed.subjects[0].bounds==first.subjects[0].bounds
    assert result.last_analysis_monotonic==100.25


def test_capture_alignment_requires_recent_pose_history_and_resets_on_rearm():
    from production_hub.tracking.frame_alignment import PtzFrameAlignment
    from production_hub.tracking.ptz_geometry import PtzMotorPose
    from production_hub.video.models import VideoSourceKey
    from testing.harness.ptz_tracking import build_geometry, _detector_snapshot
    aligner=PtzFrameAlignment();pose=PtzMotorPose(42400,34964,3450)
    snapshot=_detector_snapshot(VideoSourceKey.PTZ,[(1,NormalizedRect(.4,.2,.2,.5),.9,4)],99)
    assert aligner.align(snapshot,build_geometry(),pose,100) is snapshot
    aligner.reset()
    assert not aligner.poses
    assert aligner.align(snapshot,build_geometry(),pose,102) is snapshot


def test_calibration_measures_smaller_motor_travel_at_telephoto():
    from production_hub.tracking.ptz_geometry import PtzMotorPose
    from testing.harness.ptz_tracking import build_geometry
    geometry=build_geometry()
    wide=geometry.image_axis_motor_units(PtzMotorPose(42400,34964,2500))
    tele=geometry.image_axis_motor_units(PtzMotorPose(42400,34964,3450))
    assert 0<tele[0]<wide[0]
    assert 0<tele[1]<wide[1]


def test_calibrated_small_corrections_keep_edge_authority_and_immediate_halt():
    from production_hub.core.config.models import PtzAutomationConfig
    from production_hub.tracking.ptz_geometry import PtzMotorPose
    from production_hub.tracking.velocity_controller import SmoothPtzVelocityController
    config=PtzAutomationConfig();pose=PtzMotorPose(42400,34964,3450)
    peaks=[]
    for center in (.66,.98):
        controller=SmoothPtzVelocityController()
        commands=[controller.update(actual=pose,desired=pose,config=config,now=i*.25,
                  live_bounds=NormalizedRect(center-.05,.2,.1,.56),
                  ease_motion=True,responsive_tracking=True,image_motion_scale=(.25,.25))
                  for i in range(10)]
        peaks.append(max(abs(c.pan-50) for c in commands))
        assert not controller.halt().moving
    assert peaks[0]<peaks[1]


def test_analysis_preserves_capture_time_separately_from_completion(monkeypatch):
    from types import SimpleNamespace
    from PySide6.QtGui import QImage
    from production_hub.core.config.models import CameraTrackingConfig
    from production_hub.tracking.service import PersonTrackingService
    from production_hub.video.models import VideoSourceKey
    tracking=PersonTrackingService(None,CameraTrackingConfig())
    detector=SimpleNamespace(backend_name='test',detect=lambda image:[
        PersonCandidate(NormalizedRect(.4,.2,.2,.5),.9)])
    times=iter([100.,100.2])
    monkeypatch.setattr('production_hub.tracking.service.time.monotonic',lambda:next(times))
    tracking._analyze(VideoSourceKey.PTZ,7,QImage(10,10,QImage.Format.Format_RGB32),detector,
                      captured_monotonic=99.8)
    result=tracking.snapshot(VideoSourceKey.PTZ)
    assert result.frame_captured_monotonic==99.8
    assert result.last_analysis_monotonic==100.2
    assert result.subjects[0].last_seen_monotonic==100.2


def test_delayed_telephoto_walk_retains_subject_and_settles_in_both_directions():
    from dataclasses import replace
    from testing.harness.stage_simulation import (
        make_case,DT,DelayedPanasonic,ObservationEffects,evaluate,
    )
    from testing.harness.ptz_tracking import run_scenario
    for direction in (-1,1):
        case,_=make_case('delayed-telephoto-regression',speed=.5,direction=direction)
        case=replace(case,motion_easing_enabled=True,tracking_refinements_enabled=True)
        result=run_scenario(case,decision_interval=DT,
                            camera_factory=lambda pose:DelayedPanasonic(pose),
                            observation_transform=ObservationEffects(latency=.25))
        assert evaluate(result,case)['passed']


def test_source_guided_solo_path_retains_subject_with_delayed_feedback():
    from dataclasses import replace
    from testing.harness.stage_simulation import (
        recorded_case,DT,DelayedPanasonic,ObservationEffects,evaluate,
    )
    from testing.harness.ptz_tracking import run_scenario
    case,_=recorded_case('recorded-lectern-step',count=1,playback=1)
    case=replace(case,motion_easing_enabled=True,tracking_refinements_enabled=True)
    result=run_scenario(case,decision_interval=DT,
                       camera_factory=lambda pose:DelayedPanasonic(pose),
                       observation_transform=ObservationEffects(latency=.25))
    assert evaluate(result,case)['passed']
