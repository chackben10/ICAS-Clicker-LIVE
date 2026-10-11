"""Group framing behaviour for two and three people in Subject Tracking.

Subject Tracking works well for one person and badly for a group. The tests
below separate the two halves of that: what already works and must keep
working, and what the multi-person plan is meant to deliver. The second group
is marked `xfail(strict=True)`, so each one flips to a hard failure the moment
its fix lands and the marker has to come off deliberately.

These are written as plain pytest functions rather than in the repository's
usual unittest style because `strict` xfail is not honoured on
`unittest.TestCase` methods, and a silently-passing xfail would defeat the
point.
"""

from __future__ import annotations

import pytest

from testing.harness.ptz_tracking import (
    Scenario,
    ScenarioRecording,
    StepRecord,
    person,
    run_scenario,
    scenario,
)

# The composition an operator hands over: subjects filling 83% of frame height.
REFERENCE_OCCUPANCY = 0.83

# A group pinned to the frame edges has no side margin and reads as a crop on
# air. Real group framing keeps a visible gap.
MINIMUM_SIDE_MARGIN = 0.02


def _standing_trio() -> list:
    return [person("a", x=0.26), person("b", x=0.34), person("c", x=0.42)]


def _pair() -> list:
    return [person("left", x=0.30), person("right", x=0.38)]


def horizontal_span(step: StepRecord) -> tuple[float, float] | None:
    if not step.ptz_detections:
        return None
    return (
        min(item[0] for item in step.ptz_detections),
        max(item[0] + item[2] for item in step.ptz_detections),
    )


def zoom_direction_changes(recording: ScenarioRecording) -> int:
    """How often the zoom motor reverses -- visible on air as hunting."""

    directions = [
        (step.zoom_speed > 50) - (step.zoom_speed < 50)
        for step in recording.steps
        if step.zoom_speed != 50
    ]
    return sum(
        1
        for previous, current in zip(directions, directions[1:])
        if previous != current
    )


def held_still(recording: ScenarioRecording) -> bool:
    return all(
        (step.pan_speed, step.tilt_speed, step.zoom_speed) == (50, 50, 50)
        for step in recording.steps
    )


def group_scenario(name: str, steps, **options) -> Scenario:
    options.setdefault("occupancy", REFERENCE_OCCUPANCY)
    return scenario(name, steps, **options)


# --- behaviour that already works and must not regress ----------------------


def test_two_stable_people_hold_the_operator_shot() -> None:
    recording = run_scenario(group_scenario("pair_hold", [_pair()] * 8))
    assert recording.armed
    assert held_still(recording), (
        "A well-composed two-person shot must not be nudged; "
        f"speeds were {[(s.pan_speed, s.tilt_speed, s.zoom_speed) for s in recording.steps]}"
    )


def test_every_visible_person_is_locked_at_arm_time() -> None:
    recording = run_scenario(group_scenario("trio_roster", [_standing_trio()] * 4))
    assert len(recording.steps[0].target_ids) == 3


def test_podium_framing_never_engages_for_a_group() -> None:
    pair = [person("left", x=0.47, floor=0.62), person("right", x=0.53, floor=0.62)]
    recording = run_scenario(group_scenario("pair_on_podium", [pair] * 8))
    assert not any(step.podium_framing for step in recording.steps), (
        "Podium framing is an intimate single-subject composition."
    )


def test_mixed_posture_group_holds_a_composed_shot() -> None:
    group = [
        person("stand", x=0.28),
        person("sit", pose="seated", x=0.34),
        person("hand", pose="raised_hand", x=0.40),
    ]
    recording = run_scenario(group_scenario("mixed_postures", [group] * 10))
    assert held_still(recording)
    for step in recording.steps:
        assert len(step.ptz_detections) == 3


# --- behaviour the multi-person plan must deliver ---------------------------


def test_group_filling_the_frame_is_not_cropped() -> None:
    """Delivered by Phase 1: the group envelope has its own width budget."""

    recording = run_scenario(group_scenario("trio_wide", [_standing_trio()] * 20))
    for step in recording.steps:
        span = horizontal_span(step)
        assert span is not None
        left, right = span
        assert left >= MINIMUM_SIDE_MARGIN and right <= 1.0 - MINIMUM_SIDE_MARGIN, (
            f"step {step.step}: the group spans [{left:.3f}, {right:.3f}] with no "
            "side margin; the outer people are cropped."
        )


def test_group_framing_does_not_hunt() -> None:
    """Delivered by Phase 1: settling on width removes the edge limit cycle."""

    recording = run_scenario(group_scenario("trio_hunt", [_standing_trio()] * 24))
    assert zoom_direction_changes(recording) <= 1, (
        "The zoom reversed direction "
        f"{zoom_direction_changes(recording)} times on a completely static group."
    )


def test_person_missed_on_the_arm_frame_still_joins_the_roster() -> None:
    """Delivered by Phase 2: the roster forms over a short window, not a frame."""

    trio = _standing_trio()
    steps = [[trio[0], trio[1], person("c", x=0.42, seen_by_ptz=False)]] + [trio] * 10
    recording = run_scenario(group_scenario("late_arrival", steps))
    assert len(recording.final.target_ids) == 3, (
        "A person who was visible one frame after arming is part of the group "
        "the operator framed."
    )


def _walks_out_then_stops(steps_walking: int = 16, steps_settling: int = 40):
    """One of three walks right until the PTZ view can no longer hold them.

    `seen_by_ptz=False` would not do: that models a detector miss, and the
    person is still geometrically inside the frame, so projecting them back
    correctly asks for no widening at all. Leaving the view has to be a real
    change of position.
    """

    trio = _standing_trio()
    walking = [
        [trio[0], trio[1], person("c", x=0.42 + 0.012 * index)]
        for index in range(steps_walking)
    ]
    stopped = [
        [trio[0], trio[1], person("c", x=0.42 + 0.012 * (steps_walking - 1))]
    ] * steps_settling
    return [trio] * 3 + walking + stopped


def test_group_widens_when_a_member_walks_out_of_the_ptz_frame() -> None:
    """Delivered by Phase 3: widening is allowed while part of the group is in shot."""

    recording = run_scenario(group_scenario("member_walks_out", _walks_out_then_stops()))
    assert min(step.zoom for step in recording.steps) < recording.steps[3].zoom, (
        "With a locked person outside the frame the camera must widen to "
        "recover them, not hold its zoom."
    )


def test_recovering_one_member_never_pushes_the_others_out_of_shot() -> None:
    """Pan is far faster than zoom on this camera.

    Aiming at a group envelope the frame cannot yet hold walks the near side of
    the group out of the other edge, which is the failure the whole feature
    exists to prevent. The camera must widen first and recentre afterwards.
    """

    recording = run_scenario(group_scenario("no_abandonment", _walks_out_then_stops()))
    assert min(len(step.ptz_detections) for step in recording.steps) >= 2, (
        "The two people who never moved were pushed out of frame while the "
        "camera chased the third."
    )
    assert len(recording.final.ptz_detections) == 3, (
        "Once the walker stopped, all three should be back in shot."
    )
    assert len({step.zoom for step in recording.steps[-8:]}) == 1, (
        "The recovered shot should settle rather than keep hunting."
    )


def test_image_space_control_survives_a_missed_detection() -> None:
    """Delivered by Phase 3: partial visibility is a framing state, not a loss.

    Audience handoff is single-subject recovery for a PTZ image with nobody
    locked in it. Dropping into it because one of three blinked out also threw
    away the zoom, which is what stopped the shot correcting itself.
    """

    trio = _standing_trio()
    steps = [trio] * 3 + [[trio[0], trio[1], person("c", x=0.42, seen_by_ptz=False)]] * 8
    recording = run_scenario(group_scenario("partial_visibility", steps))
    assert not any(
        step.tracking_source == "audience_handoff" for step in recording.steps
    ), (
        "Two of three people are still in the PTZ image; that is a framing "
        "problem, not a lost-subject recovery."
    )
    assert all(len(step.target_ids) == 3 for step in recording.steps[4:]), (
        "The missed person stays part of the composition."
    )


def test_a_single_missed_frame_does_not_drop_a_member() -> None:
    """Delivered by Phase 4: a briefly-missed member keeps their place."""

    trio = _standing_trio()
    blink = [trio[0], trio[1], person("c", x=0.42, seen_by_ptz=False, seen_by_audience=False)]
    steps = [trio] * 4 + [blink] + [trio] * 6
    recording = run_scenario(group_scenario("blink", steps))
    for step in recording.steps:
        assert len(step.target_ids) == 3, (
            f"step {step.step}: one missed detector frame dropped a person from "
            "the shot."
        )


def test_a_blink_does_not_halt_a_panning_camera() -> None:
    """A missed detection used to change the target count, which reset the
    framing dwell and stopped continuous motion for a third of a second --
    several times a minute with three people in shot."""

    def walking(index: int, blink: bool):
        return [
            person("a", x=0.26 + 0.006 * index),
            person("b", x=0.34 + 0.006 * index),
            person(
                "c",
                x=0.42 + 0.006 * index,
                seen_by_ptz=not blink,
                seen_by_audience=not blink,
            ),
        ]

    recording = run_scenario(
        group_scenario(
            "walk_with_blink",
            [walking(index, blink=index in (8, 9)) for index in range(20)],
        )
    )
    moving = recording.steps[6:]
    assert all(step.pan_speed != 50 for step in moving), (
        "The camera stopped mid-pan: "
        f"{[(s.step, s.pan_speed) for s in moving if s.pan_speed == 50]}"
    )


def test_departed_member_is_retired_so_the_shot_settles() -> None:
    """Delivered by Phase 2: an absent lock is retired after the timeout.

    The scenario has to outlast `subject_lock_retire_seconds` and then leave
    the camera time to recompose, so it runs well past the departure rather
    than sampling the moment of it.
    """

    trio = _standing_trio()
    walking_out = [
        [
            trio[0],
            trio[1],
            person("c", x=0.42 + 0.01 * index, seen_by_ptz=False, seen_by_audience=index < 3),
        ]
        for index in range(70)
    ]
    recording = run_scenario(group_scenario("member_departs", [trio] * 3 + walking_out))
    assert len(recording.final.target_ids) == 2, (
        "The person who left should no longer be part of the shot."
    )
    assert held_still(ScenarioRecording("tail", True, "", recording.steps[-6:])), (
        "Once a locked person is gone the camera must settle on the people who "
        "remain."
    )


def test_a_pair_walking_apart_is_not_lost_entirely() -> None:
    """Both members leave the frame on opposite sides at the same moment.

    Widening is the only recovery available -- panning cannot reach both -- and
    it is exactly what the full-loss branch forbids.
    """

    walking = [
        [person("left", x=0.30 + 0.006 * index), person("right", x=0.38 - 0.006 * index)]
        for index in range(20)
    ]
    settling = [
        [person("left", x=0.30 + 0.006 * 19), person("right", x=0.38 - 0.006 * 19)]
    ] * 24
    recording = run_scenario(group_scenario("walking_apart", walking + settling))
    assert len(recording.final.ptz_detections) == 2, (
        "Both people ended up outside the frame with the camera holding its "
        "zoom, so neither was recovered."
    )


def test_recovery_widening_is_bounded_by_the_audience_estimate() -> None:
    """The wide camera is what keeps recovery honest.

    With nobody locked visible to the PTZ there is no optical scale to measure,
    so an unbounded widen would end at a stage-wide shot. The Audience view
    still knows roughly where these people are, and the calibrated framing of
    those positions is the exact stopping point: open up enough to find them,
    and not one step further.
    """

    from testing.harness.ptz_tracking import (
        build_geometry,
        union_rect,
    )

    pair = [person("left", x=0.24), person("right", x=0.44)]
    lost = [
        [
            person("left", x=0.24, seen_by_ptz=False),
            person("right", x=0.44, seen_by_ptz=False),
        ]
    ] * 80
    recording = run_scenario(
        group_scenario("full_loss", [[person("left", x=0.30), person("right", x=0.38)]] * 3 + lost)
    )
    bound = build_geometry().pose_for_target(
        union_rect([item.rect for item in pair]),
        desired_frame_height=0.70,
        desired_frame_width=0.92,
    )
    zooms = [step.zoom for step in recording.steps]

    assert min(zooms) < zooms[0], "the camera never widened to look for them"
    # Panasonic zoom counts up toward telephoto, so a wider shot is a lower
    # number and the bound is a floor.
    assert min(zooms) >= bound.zoom, (
        f"widened past the Audience estimate: reached {min(zooms)}, bound {bound.zoom}"
    )
    assert len(set(zooms[-12:])) == 1, "the recovery widening never settled"


def test_pair_widens_apart_then_tightens_together_with_renewed_detection_ids() -> None:
    def pair(separation, suffix=0):
        return [
            person("left", x=.34-separation/2, detector_suffix=suffix),
            person("right", x=.34+separation/2, detector_suffix=suffix),
        ]
    steps = [pair(.08)] * 4
    steps += [pair(.08 + i*.005, i//4) for i in range(32)]
    steps += [pair(.235, 8)] * 60
    wide_end = len(steps) + 2
    steps += [pair(.235 - i*.005, 9+i//4) for i in range(32)]
    steps += [pair(.08, 17)] * 100
    recording = run_scenario(group_scenario("pair_apart_and_together", steps))
    wide_zoom = recording.steps[wide_end-1].zoom
    assert wide_zoom < recording.steps[0].zoom - 50
    assert recording.final.zoom > wide_zoom + 50
    assert all(len(s.target_ids) == 2 for s in recording.steps)
    assert len(recording.final.ptz_detections) == 2


def test_outside_group_projection_keeps_full_width_when_it_crosses_top_edge() -> None:
    from production_hub.core.config.models import PtzAutomationConfig
    from production_hub.tracking.framing import _adaptive_ptz_composition
    from production_hub.tracking.models import NormalizedRect

    target, _, _ = _adaptive_ptz_composition(
        PtzAutomationConfig(),
        [NormalizedRect(.20, .10, .15, .60), NormalizedRect(1.2, -.05, .15, .60)],
        podium=False, allow_outside_frame=True,
    )
    assert target.x + target.width >= 1.35
    assert target.width >= 1.15


def test_regrouped_pair_tightens_to_configured_group_size() -> None:
    from production_hub.core.config.models import PtzAutomationConfig
    from production_hub.tracking.framing import _adaptive_ptz_composition
    from production_hub.tracking.models import NormalizedRect

    # Observed in the live test after the pair returned together: a torso
    # detection beside a fuller-body detection, with ample horizontal room.
    config = PtzAutomationConfig()
    target, height, width = _adaptive_ptz_composition(
        config, [NormalizedRect(.36, .17, .09, .38), NormalizedRect(.50, .22, .14, .62)],
        podium=False,
    )
    ratio = max(target.height/height, target.width/width)
    assert height >= config.group_frame_height
    assert ratio < .97, 'The returned pair is still held at the looser recovery size'
