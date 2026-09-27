"""Recorded box failures and delayed camera feedback regressions for Dev."""
import pytest
from production_hub.tracking.apple_vision import merge_pose_envelopes
from production_hub.tracking.models import NormalizedRect, PersonCandidate
from production_hub.tracking.velocity_controller import _safe_zone_target, _safe_zone_ease, SmoothPtzVelocityController
from production_hub.tracking.ptz_geometry import PtzMotorPose
from production_hub.core.config.models import PtzAutomationConfig
from testing.harness.ptz_tracking import person, pose_framing, scenario, run_scenario


def merge(rectangles, poses):
    return merge_pose_envelopes([PersonCandidate(NormalizedRect(*r), .9) for r in rectangles],
                                [NormalizedRect(*p) for p in poses], conservative=True)


def test_recorded_drum_kit_pose_cannot_pull_edge_subject_to_center():
    rectangle = (.8779,.0428,.1192,.7164)
    result = merge([rectangle], [(.4112,.0933,.5888,.744)])
    assert len(result) == 1
    assert result[0].bounds == NormalizedRect(*rectangle)


def test_recorded_outstretched_arms_remain_included_without_duplicate_person():
    result = merge([(.2833,.1923,.4326,.6345), (.3843,.195,.2307,.5844)],
                   [(.2053,.2426,.591,.6793)])
    assert len(result) == 1
    assert result[0].bounds.x == pytest.approx(.2053)
    assert result[0].bounds.width == pytest.approx(.591)


def test_distinct_people_and_standalone_pose_are_retained():
    result = merge([(.2,.2,.15,.6),(.4,.2,.15,.6)],
                   [(.19,.1,.17,.7),(.39,.1,.17,.7),(.8,.2,.1,.4)])
    assert len(result) == 3
    assert result[0].bounds.y == .1
    assert result[1].bounds.y == .1


def test_pose_enrichment_does_not_chain_across_multiple_poses():
    result = merge([(.4,.2,.15,.6)], [(.39,.15,.17,.65),(.30,.2,.3,.6)])
    assert len(result) == 1
    assert result[0].bounds.x == .39


def test_safe_zone_is_stationary_and_speed_increases_symmetrically_to_edges():
    assert all(_safe_zone_target(x,.06,44) == 0 for x in (-.06,0,.06))
    speeds = [_safe_zone_target(x,.06,44) for x in (.07,.14,.25,.35,.44)]
    assert all(a < b for a,b in zip(speeds,speeds[1:]))
    assert speeds[-1] == 44
    assert [_safe_zone_target(-x,.06,44) for x in (.07,.14,.25,.35,.44)] == [-s for s in speeds]


def test_easing_brakes_before_reversing_and_ramps_in_and_out():
    assert 0 < _safe_zone_ease(0,20,.1) < 20
    assert 0 < _safe_zone_ease(20,0,.1) < 20
    assert 0 < _safe_zone_ease(20,-20,.1) < 20


@pytest.mark.parametrize('group',[False,True])
@pytest.mark.parametrize('zoom,limit',[(2400,44),(3452,26)])
def test_safe_zone_controller_eases_stops_and_preserves_zoom(group,zoom,limit):
    controller=SmoothPtzVelocityController()
    commands=[]
    for i,x in enumerate([.94]*16+[.5]*20):
        commands.append(controller.update(actual=PtzMotorPose(43000,34000,zoom),
            desired=PtzMotorPose(43000,34000,zoom), config=PtzAutomationConfig(), now=i*.25,
            live_bounds=NormalizedRect(x-.04,.43,.08,.10), group_tracking=group,
            ease_motion=True,responsive_tracking=True,safe_zone_control=True))
    assert 0 < commands[0].pan-50 < limit
    assert commands[15].pan-50 == limit
    assert 50 < commands[16].pan < commands[15].pan
    assert commands[-1].pan == 50
    assert all(c.zoom == 50 for c in commands)
    assert not controller.halt().moving


@pytest.mark.parametrize('delay',[0,1,2])
@pytest.mark.parametrize('step',[.016,.03])
def test_delayed_feedback_step_settles_without_losing_subject(delay,step):
    p=person('speaker',x=.34)
    frames=[[p]]*8+[[p.moved(x=p.x+step)]]*70
    recording=run_scenario(scenario('safe-zone-delayed-step',frames,
        initial_pose=pose_framing(frames[0],occupancy=.83), motion_easing_enabled=True,
        tracking_refinements_enabled=True,safe_zone_tracking_enabled=True,
        observation_delay_steps=delay))
    assert all(len(s.ptz_detections)==1 for s in recording.steps)
    if step < .02:
        # A small step inside the framed zone: the camera stays put rather
        # than creeping after it (it moves only on a clear drift off centre).
        assert all(s.pan_speed == 50 for s in recording.steps[8:])
    else:
        assert any(s.pan_speed != 50 for s in recording.steps)
    assert all(s.pan_speed==s.tilt_speed==50 for s in recording.steps[-16:])
    x,y,w,h=recording.final.ptz_detections[0]
    from production_hub.tracking.velocity_controller import FRAMED_DRIFT
    assert abs(x+w/2-.5) < (FRAMED_DRIFT if step < .02 else .12)


@pytest.mark.parametrize('count',[1,2])
def test_safe_zone_brisk_walk_keeps_single_and_group_visible(count):
    people=[person(str(i),x=.30+i*.04) for i in range(count)]
    moving=[[p.moved(x=p.x+.03*j) for p in people] for j in range(12)]
    frames=[people]*8+moving+[moving[-1]]*60
    recording=run_scenario(scenario('safe-zone-brisk-walk',frames,
        initial_pose=pose_framing(frames[0],occupancy=.83),motion_easing_enabled=True,
        tracking_refinements_enabled=True,safe_zone_tracking_enabled=True))
    assert all(len(s.ptz_detections)==count for s in recording.steps)
    assert all(s.pan_speed==s.tilt_speed==50 for s in recording.steps[-16:])
