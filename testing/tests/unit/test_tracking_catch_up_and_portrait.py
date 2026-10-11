"""Dev pursuit and two-person composition, with camera motion in the loop."""
import pytest

from production_hub.core.config.models import PtzAutomationConfig
from production_hub.tracking.framing import _adaptive_ptz_composition, _portrait_subject_bounds
from production_hub.tracking.models import NormalizedRect
from production_hub.tracking.ptz_geometry import PtzMotorPose
from production_hub.tracking.velocity_controller import SmoothPtzVelocityController
from testing.harness.ptz_tracking import build_geometry, person, pose_framing, run_scenario, scenario


def run(frames, *, refined=True, occupancy=.83):
    return run_scenario(scenario(
        'dev-pursuit-and-portrait', frames,
        initial_pose=pose_framing(frames[0], occupancy=occupancy),
        motion_easing_enabled=True, tracking_refinements_enabled=refined,
    ))


@pytest.mark.parametrize('count', [1, 2])
@pytest.mark.parametrize('axis,rate', [('pan', .03), ('tilt', .018)])
def test_brisk_walk_keeps_people_visible_and_stops_afterward(count, axis, rate):
    people = [person(str(i), x=.30 + i*.04) for i in range(count)]
    moving = [[p.moved(x=p.x+rate*j) if axis == 'pan' else p.moved(floor=p.floor-rate*j)
               for p in people] for j in range(12)]
    frames = [people]*8 + moving + [moving[-1]]*40
    refined, before = run(frames), run(frames, refined=False)
    assert refined.armed
    assert all(len(s.ptz_detections) == count for s in refined.steps)
    assert all(len(s.target_ids) == count for s in refined.steps)
    new_peak = max(abs(getattr(s, axis+'_speed')-50) for s in refined.steps)
    old_peak = max(abs(getattr(s, axis+'_speed')-50) for s in before.steps)
    assert new_peak > old_peak
    assert all(s.pan_speed == s.tilt_speed == 50 for s in refined.steps[-8:])
    if axis == 'pan' or count == 1:
        assert any(len(s.ptz_detections) < count for s in before.steps), 'Scenario must reproduce the previous loss'


@pytest.mark.parametrize('group', [False, True])
@pytest.mark.parametrize('direction', [-1, 1])
def test_edge_pursuit_reaches_higher_speeds_and_only_widens(group, direction):
    # Dev pursuit (picture speed, measured AW model) must out-run the release
    # path toward a subject at the frame corner, and may only *widen* there:
    # tightening on someone leaving the shot is what lost them live.
    controllers = [SmoothPtzVelocityController(), SmoothPtzVelocityController()]
    records = []
    for refined, controller in zip([False, True], controllers):
        commands = [controller.update(
            actual=PtzMotorPose(43000, 34000, 2400), desired=PtzMotorPose(43000, 34000, 2000),
            config=PtzAutomationConfig(), now=i*.25,
            live_bounds=NormalizedRect(.50+direction*.38-.04, .48+direction*.38-.05, .08, .10),
            group_tracking=group, ease_motion=True, responsive_tracking=refined,
        ) for i in range(12)]
        records.append(commands)
    old, new = records
    assert max(abs(c.pan-50) for c in new[:4]) > max(abs(c.pan-50) for c in old[:4])
    # Aim follows the subject: right of centre -> pan above 50; low -> tilt below 50.
    assert all((c.pan-50)*direction > 0 for c in new)
    assert all((c.tilt-50)*direction < 0 for c in new)
    assert all(c.zoom <= 50 for c in new) and any(c.zoom < 50 for c in new)
    assert not controllers[1].halt().moving


def test_small_corrections_stay_gentle_and_stop():
    # A tenth-of-a-frame re-frame is centred at a crawl, not with a lunge,
    # and a centred, still subject never moves the camera.
    controller = SmoothPtzVelocityController()
    commands = [controller.update(
        actual=PtzMotorPose(43000,34000,2400), desired=PtzMotorPose(43000,34000,2400),
        config=PtzAutomationConfig(), now=i*.25,
        live_bounds=NormalizedRect(x-.06,.20,.12,.56),
        ease_motion=True, responsive_tracking=True,
    ) for i,x in enumerate([.50]*8+[.60]*8+[.50]*12)]
    assert all(c.pan == 50 for c in commands[:8])
    assert all(0 < c.pan-50 <= 22 for c in commands[8:16])
    assert not commands[-1].moving


def pair(separation, suffix=0):
    return [person('left',x=.34-separation/2,detector_suffix=suffix),
            person('right',x=.34+separation/2,detector_suffix=suffix)]


def test_separation_then_regrouping_returns_to_a_tight_stable_two_shot():
    frames = [pair(.05)]*40
    frames += [pair(.05+i*.005,i//4) for i in range(32)]
    frames += [pair(.205,8)]*40
    wide_end = len(frames)+2
    frames += [pair(.205-i*.005,9+i//4) for i in range(32)]
    frames += [pair(.05,17)]*100
    recording = run(frames)
    assert all(len(s.target_ids) == 2 and len(s.ptz_detections) == 2 for s in recording.steps)
    assert recording.steps[wide_end-1].zoom < recording.steps[40].zoom-200
    assert recording.final.zoom > recording.steps[wide_end-1].zoom+500
    assert recording.steps[wide_end+32+32].zoom == recording.final.zoom, 'Regrouping must settle within 8 seconds'
    assert len({s.zoom for s in recording.steps[-40:]}) == 1
    for x,y,width,height in recording.final.ptz_detections:
        assert .03 < x and x+width < .97
        assert .02 < y < .20
        assert width > .30, 'Both people should reach the closer reference-shot scale'
    assert recording.final.pan_speed == recording.final.tilt_speed == 50


def test_brief_detector_miss_does_not_pump_the_portrait_zoom():
    people = pair(.04)
    frames = [people]*70 + [[people[0],people[1].moved(seen_by_ptz=False,seen_by_audience=False)]]*2 + [people]*40
    recording = run(frames)
    assert len({s.zoom for s in recording.steps[60:]}) == 1
    assert all(len(s.target_ids) == 2 for s in recording.steps)


def test_portrait_scale_does_not_shrink_again_when_legs_leave_the_frame():
    full = NormalizedRect(.30,.08,.25,1.10)
    clipped = NormalizedRect(.30,.08,.25,.92)
    assert _portrait_subject_bounds(full) == _portrait_subject_bounds(clipped)
    torso = NormalizedRect(.30,.08,.25,.40)
    assert _portrait_subject_bounds(torso) == torso


def test_photo_reference_remains_width_limited_with_both_heads_included():
    # Approximate visible body rectangles from the user's reference photo.
    subjects = [NormalizedRect(.145,.145,.265,.49), NormalizedRect(.685,.06,.295,.94)]
    prepared = [_portrait_subject_bounds(bounds) for bounds in subjects]
    target, height, width = _adaptive_ptz_composition(
        PtzAutomationConfig(), prepared, podium=False, two_person_portrait=True,
    )
    footprint = max(target.height/height, target.width/width)
    zoom = build_geometry().zoom_for_relative_footprint(2900, footprint)
    assert abs(zoom-2900) <= PtzAutomationConfig().zoom_deadband_units, 'The reference shot should hold within zoom tolerance'
    assert target.width >= .83
    assert all(p.y == original.y for p,original in zip(prepared,subjects))


def test_portrait_projection_preserves_a_missing_person_outside_the_frame():
    subjects = [NormalizedRect(.2,.1,.15,.6), NormalizedRect(1.2,-.05,.15,.6)]
    target, height, width = _adaptive_ptz_composition(
        PtzAutomationConfig(), [_portrait_subject_bounds(s) for s in subjects],
        podium=False, allow_outside_frame=True, two_person_portrait=True,
    )
    assert target.x+target.width >= 1.35
    assert max(target.height/height,target.width/width) > 1.2


@pytest.mark.parametrize('group', [False, True])
@pytest.mark.parametrize('bounds', [True, False])
def test_recorded_telephoto_range_stays_within_a_followable_picture_speed(group, bounds):
    # The live failure at zoom 3452-3663 hit AW pan 6/94 and lost the person;
    # the release cap of 26 then could not keep up with a walker at all
    # (~0.23 frames/s there). Dev bounds *picture* speed instead: enough to
    # follow, never a whip -- including Audience recovery and reversals.
    from production_hub.integrations.panasonic_awp import speed_model
    controller = SmoothPtzVelocityController()
    xs = [.5]*4+[.8]*4+[.2]*4+[.85]*4+[.5]*20
    commands = []
    for i,x in enumerate(xs):
        commands.append(controller.update(
            actual=PtzMotorPose(43800,33300,3452),
            desired=PtzMotorPose(43800+round((x-.5)*6000),33300,3452),
            config=PtzAutomationConfig(),now=i*.25,
            live_bounds=NormalizedRect(x-.06,.2,.12,.56) if bounds else None,
            group_tracking=group,ease_motion=True,responsive_tracking=True,
        ))
    cap = .60 if not bounds else 1.0
    assert max(abs(speed_model.picture_speed('pan', c.pan-50, 3452)) for c in commands) <= cap+.02
    assert all(abs(c.pan-50) < 45 for c in commands)
    # Each held offset is followed in its own direction once the ramp settles.
    # Image tracking: subject right -> AW pan above 50. Recovery: target pan
    # above the current one -> Panasonic pan must rise -> AW pan below 50.
    for start, x in ((7, .8), (11, .2), (15, .85)):
        side = 1 if x > .5 else -1
        expected = side if bounds else -side
        assert (commands[start].pan-50)*expected > 0
    assert not commands[-1].moving