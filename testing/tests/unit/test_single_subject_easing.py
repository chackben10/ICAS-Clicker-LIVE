"""Dev motion tests: scripted image observations and closed-loop camera motion."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from production_hub.app import main as app_main
from production_hub.app.dev_launcher import DEV_CHILD_ARGUMENT
from production_hub.core.config.models import CameraTrackingConfig
from production_hub.tracking.models import NormalizedRect
from production_hub.tracking.ptz_geometry import PtzMotorPose
from production_hub.tracking.velocity_controller import SmoothPtzVelocityController
from testing.harness.ptz_tracking import person, pose_framing, run_scenario, scenario


def _record(frames, *, eased=True):
    return run_scenario(scenario(
        'single-person-easing', frames,
        initial_pose=pose_framing(frames[0], occupancy=.83),
        motion_easing_enabled=eased,
    ))


def _deltas(recording, axis='pan_speed'):
    return [getattr(step, axis) - 50 for step in recording.steps]


@pytest.mark.parametrize('direction', [-1, 1])
def test_small_step_eases_to_a_stop_without_a_reverse_correction(direction):
    speaker = person('speaker', x=.34)
    frames = [[speaker]] * 8 + [[speaker.moved(x=.34 + direction * .016)]] * 40
    eased, legacy = _record(frames), _record(frames, eased=False)
    speeds = [direction * speed for speed in _deltas(eased)]
    assert eased.armed
    assert max(speeds) < max(abs(speed) for speed in _deltas(legacy))
    assert min(speeds) >= 0
    peak = speeds.index(max(speeds))
    tail = speeds[peak:]
    assert all(a >= b for a, b in zip(tail, tail[1:]))
    assert len({speed for speed in tail if speed > 0}) >= 3
    assert speeds[-8:] == [0] * 8
    box = eased.final.ptz_detections[0]
    assert abs(box[0] + box[2] / 2 - .50) <= .08


def test_brief_duck_has_a_gentler_tilt_and_settles_without_hunting():
    speaker = person('speaker', x=.34)
    frames = [[speaker]] * 8 + [[speaker.moved(floor=speaker.floor + .024)]] * 6 + [[speaker]] * 40
    eased, legacy = _record(frames), _record(frames, eased=False)
    speeds = _deltas(eased, 'tilt_speed')
    old = _deltas(legacy, 'tilt_speed')
    assert max(abs(v) for v in speeds) < max(abs(v) for v in old)
    assert max(abs(b-a) for a,b in zip(speeds,speeds[1:])) < max(abs(b-a) for a,b in zip(old,old[1:]))
    assert speeds[-8:] == [0] * 8
    box = eased.final.ptz_detections[0]
    assert abs(box[1] + box[3] / 2 - .48) <= .11


def test_large_move_keeps_catch_up_and_settles_in_frame():
    speaker = person('speaker', x=.34)
    frames = [[speaker]] * 8 + [[speaker.moved(x=.385)]] * 40
    eased, legacy = _record(frames), _record(frames, eased=False)
    assert _deltas(eased)[10:13] == _deltas(legacy)[10:13]
    assert _deltas(eased)[-8:] == [0] * 8
    assert all(step.ptz_detections for step in eased.steps)
    box = eased.final.ptz_detections[0]
    assert abs(box[0] + box[2] / 2 - .50) <= .08


def _commands(*, eased, group=False, bounds=True):
    controller = SmoothPtzVelocityController()
    commands = []
    for i, x in enumerate([.50]*4 + [.65]*6 + [.50]*12):
        commands.append(controller.update(
            actual=PtzMotorPose(43000, 34000, 2200),
            desired=PtzMotorPose(43300, 34200, 2000),
            config=CameraTrackingConfig().automation, now=i*.25,
            live_bounds=NormalizedRect(x-.06, .20, .12, .56) if bounds else None,
            group_tracking=group, ease_motion=eased,
        ))
    return controller, commands


@pytest.mark.parametrize('group,bounds', [(False, True), (True, True), (False, False)])
def test_easing_preserves_zoom_commands(group, bounds):
    assert [c.zoom for c in _commands(eased=True, group=group, bounds=bounds)[1]] == [c.zoom for c in _commands(eased=False, group=group, bounds=bounds)[1]]


def test_halt_is_immediate_even_while_easing():
    controller = SmoothPtzVelocityController()
    kwargs = dict(actual=PtzMotorPose(43000,34000,2200), desired=PtzMotorPose(43000,34000,2200),
                  config=CameraTrackingConfig().automation, live_bounds=NormalizedRect(.60,.30,.12,.56), ease_motion=True)
    for i in range(5):
        moving = controller.update(now=i*.25, **kwargs)
    assert moving.moving
    assert not controller.halt().moving
    kwargs['live_bounds'] = NormalizedRect(.44,.20,.12,.56)
    assert not controller.update(now=1.25, **kwargs).moving


@pytest.mark.parametrize('dev,frozen,expected', [(True,False,True), (False,False,False), (True,True,False), (False,True,False)])
def test_easing_is_enabled_only_for_source_dev_startup(monkeypatch, dev, frozen, expected):
    context = SimpleNamespace(ptz_automation=SimpleNamespace(motion_easing_enabled=False))
    monkeypatch.setattr(app_main, 'build_context', lambda _: context)
    monkeypatch.setattr(app_main, 'startup_checks', AsyncMock())
    monkeypatch.setattr(app_main.sys, 'argv', ['main.py'] + ([DEV_CHILD_ARGUMENT] if dev else []))
    monkeypatch.setattr(app_main.sys, 'frozen', frozen, raising=False)
    class StartupReached(Exception):
        pass
    def start(_):
        raise StartupReached
    monkeypatch.setattr(app_main, 'start_background_services', start)
    with pytest.raises(StartupReached):
        app_main._run(app_main.parse_args([]))
    assert context.ptz_automation.motion_easing_enabled is expected
    assert context.ptz_automation.tracking_refinements_enabled is expected


@pytest.mark.parametrize('mode', ['single', 'group', 'recovery'])
@pytest.mark.parametrize('interval', [.125, .25, .5])
def test_all_tracking_modes_taper_pan_and_tilt_to_neutral(mode, interval):
    controller = SmoothPtzVelocityController()
    speeds = []
    for i in range(round(8 / interval)):
        moving = i * interval < 3
        command = controller.update(
            actual=PtzMotorPose(43000, 34000, 2200),
            desired=PtzMotorPose(44000 if moving else 43000, 34500 if moving else 34000, 2200),
            config=CameraTrackingConfig().automation, now=i * interval,
            live_bounds=None if mode == 'recovery' else NormalizedRect(
                .76 if moving else .44, .59 if moving else .20, .12, .56),
            group_tracking=mode == 'group', ease_motion=True,
        )
        if not moving:
            speeds.append((command.pan - 50, command.tilt - 50))
    for axis in (0, 1):
        magnitudes = [abs(speed[axis]) for speed in speeds]
        assert magnitudes[0] > 0
        assert all(a >= b for a, b in zip(magnitudes, magnitudes[1:]))
        assert magnitudes[-4:] == [0] * 4
        assert [value for value in magnitudes if value][-1] == 1
        # At 2 Hz the same time-based taper has fewer sampled speed levels.
        assert len({value for value in magnitudes if value > 0}) >= 2
        if interval <= .25:
            assert len({value for value in magnitudes if 0 < value < 5}) >= 2


def test_group_ease_out_settles_with_both_people_in_frame():
    pair = [person('left', x=.32), person('right', x=.36)]
    moved = [p.moved(x=p.x + .025) for p in pair]
    recording = run_scenario(scenario(
        'group-ease-out', [pair] * 8 + [moved] * 48,
        initial_pose=pose_framing(pair, occupancy=.76), motion_easing_enabled=True,
    ))
    assert recording.armed
    assert len(recording.final.target_ids) == 2
    assert _deltas(recording)[-8:] == [0] * 8
    boxes = recording.final.ptz_detections
    assert len(boxes) == 2
    assert all(x >= 0 and x + width <= 1 for x, y, width, height in boxes)
    assert abs((min(b[0] for b in boxes) + max(b[0]+b[2] for b in boxes))/2 - .5) < .08


def test_reversal_brakes_promptly_instead_of_coasting_away():
    controllers = [SmoothPtzVelocityController(), SmoothPtzVelocityController()]
    sequences = [[], []]
    for eased, controller, commands in zip([False, True], controllers, sequences):
        for i in range(16):
            command = controller.update(
                actual=PtzMotorPose(43000, 34000, 2200),
                desired=PtzMotorPose(44000 if i < 10 else 42000, 34000, 2200),
                config=CameraTrackingConfig().automation, now=i * .25,
                live_bounds=None, ease_motion=eased,
            )
            commands.append(command.pan)
    assert sequences[0] == sequences[1]
    assert sequences[1][-1] > 50
