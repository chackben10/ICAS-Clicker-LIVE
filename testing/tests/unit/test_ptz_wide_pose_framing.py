"""Single-subject framing for postures that are wider than they are tall.

A lone speaker is bounded by their height in almost every posture, which is why
single-subject framing was tuned on the vertical axis alone. Arms spread, a wide
gesture from an upper-body detection, or leaning across a lectern are the
exceptions: the person runs out of frame sideways first. Without a horizontal
limit the controller reads them as too small, tightens until they touch the
frame edge, is pushed back by the edge rule, and tightens again -- the same
limit cycle the group envelope had, in the single-subject path.

The ceiling that fixes it can only ever make a shot wider, never tighter, so it
cannot reach a posture whose framing is already bounded by height. The golden
recording is what proves that claim across the rest of the single-subject set.
"""

from __future__ import annotations

import pytest

from testing.harness.ptz_tracking import (
    person,
    pose_framing,
    run_scenario,
    scenario,
)

REFERENCE_OCCUPANCY = 0.83
WIDE_POSES = ("arms_spread", "wide_gesture", "leaning_over_lectern")

# Long enough for the zoom to reach equilibrium; a shorter run cannot tell a
# settled shot from one still creeping toward the frame edge.
SETTLING_STEPS = 140
SETTLED_TAIL = 40


def _run(pose: str):
    standing = person("speaker", x=0.34)
    return run_scenario(
        scenario(
            f"wide_{pose}",
            [[standing]] * 3 + [[person("speaker", pose=pose, x=0.34)]] * SETTLING_STEPS,
            initial_pose=pose_framing([standing], occupancy=REFERENCE_OCCUPANCY),
        )
    )


@pytest.mark.parametrize("pose", WIDE_POSES)
def test_a_persistently_wide_pose_settles_instead_of_hunting(pose: str) -> None:
    recording = _run(pose)
    tail = recording.steps[-SETTLED_TAIL:]
    zooms = {step.zoom for step in tail}
    assert len(zooms) == 1, (
        f"{pose}: the zoom never settled, ranging over {sorted(zooms)} across the "
        "last ten seconds. That reads on air as the shot breathing."
    )


@pytest.mark.parametrize("pose", WIDE_POSES)
def test_a_wide_pose_keeps_a_side_margin(pose: str) -> None:
    recording = _run(pose)
    detection = recording.final.ptz_detections[0]
    left, width = detection[0], detection[2]
    assert left >= 0.04 and left + width <= 0.96, (
        f"{pose}: the subject spans [{left:.3f}, {left + width:.3f}] with no room "
        "for a gesture."
    )


def test_a_standing_subject_is_untouched_by_the_horizontal_limit() -> None:
    """The ceiling must be unreachable for the posture the tuning was built on."""

    standing = person("speaker", x=0.34)
    recording = run_scenario(
        scenario(
            "standing_hold",
            [[standing]] * 40,
            initial_pose=pose_framing([standing], occupancy=REFERENCE_OCCUPANCY),
        )
    )
    for step in recording.steps:
        assert (step.pan_speed, step.tilt_speed, step.zoom_speed) == (50, 50, 50)
        assert step.ptz_detections[0][2] < 0.40, (
            "a standing body never approaches the horizontal budget"
        )
