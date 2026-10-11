"""Characterization lock on single-subject PTZ tracking.

Single-person Subject Tracking is tuned behaviour, not derived behaviour. The
multi-person work changes framing maths that the single-subject path also
reaches, so this suite records what one person produces today -- pose by pose,
speed by speed -- and fails on any drift.

A diff here is not automatically a bug, but it is never an accident: either the
change was meant to reach N=1 and the recording is promoted deliberately, or
the change is mis-gated and must be reverted. Promote with:

    PH_UPDATE_PTZ_GOLDEN=1 testing/.venv/bin/python -m pytest \
        testing/tests/unit/test_ptz_single_subject_golden.py

and describe the diff in the change summary.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from testing.harness.paths import TESTING_ROOT
from testing.harness.ptz_tracking import (
    Scenario,
    person,
    pose_framing,
    run_scenario,
    scenario,
)

# The occupancy a well-composed single-subject shot settles at, derived from
# off_podium_subject_frame_height minus the subject safety padding.
REFERENCE_OCCUPANCY = 0.83

GOLDEN_PATH = TESTING_ROOT / "fixtures" / "tracking" / "ptz_single_subject_golden.json"


def _hold(subject, steps: int) -> list[list]:
    return [[subject] for _ in range(steps)]


def _scenarios() -> list[Scenario]:
    """Scenarios start from the shot an operator would actually hand over.

    x=0.34 stands on stage but clear of the podium polygon, so these cover the
    off-podium composition path; `podium_speaker` covers the other one. The
    posture scenarios begin standing and then change pose, because that is how
    they arise live: the operator frames a person, and the person moves.
    """

    speaker = person("speaker", pose="standing", x=0.34)
    framed = pose_framing([speaker], occupancy=REFERENCE_OCCUPANCY)

    def posture(name: str, pose: str) -> Scenario:
        """A posture held in a shot already composed for it.

        Framing these from the standing shot instead would drive the requested
        zoom past the calibrated telephoto limit, where every candidate framing
        rule saturates to the same clamp and the recording stops discriminating.
        """

        posed = person("speaker", pose=pose, x=0.34)
        return scenario(
            name,
            _hold(posed, 10),
            initial_pose=pose_framing([posed], occupancy=REFERENCE_OCCUPANCY),
        )

    return [
        # The shot the operator already got right. Any motion here is a
        # regression: this is the behaviour that took a month to settle.
        scenario("hold_at_reference_scale", _hold(speaker, 8), initial_pose=framed),
        # Handed over slightly wide: the tuned tightening toward reference scale.
        scenario("tighten_to_reference_scale", _hold(speaker, 10), occupancy=0.72),
        # Armed while the subject stands off to one side and low in frame.
        scenario(
            "recenter_off_center",
            _hold(person("speaker", x=0.385, floor=0.66), 14),
            initial_pose=framed,
        ),
        # A walking subject: exercises velocity smoothing and prediction.
        scenario(
            "walk_across_stage",
            [[person("speaker", x=0.26 + 0.010 * index)] for index in range(16)],
            occupancy=REFERENCE_OCCUPANCY,
        ),
        # Vision returns only head and torso for a full-height person.
        posture("upper_body_only", "upper_body"),
        # Wider than tall. These three are exactly where a naive horizontal
        # occupancy fix would leak into the single-subject path.
        posture("arms_spread_wide", "arms_spread"),
        posture("wide_gesture", "wide_gesture"),
        posture("leaning_over_lectern", "leaning_over_lectern"),
        # Raised hand reaches the top of frame and must widen, not tighten.
        scenario(
            "raised_hand",
            _hold(speaker, 2) + _hold(person("speaker", pose="raised_hand", x=0.34), 10),
            initial_pose=framed,
        ),
        # Inside the podium polygon: the intimate composition.
        scenario(
            "podium_speaker",
            _hold(person("speaker", x=0.50, floor=0.62), 10),
            occupancy=REFERENCE_OCCUPANCY,
        ),
        # Framed far too wide; the controller should tighten a long way.
        scenario("distant_small_subject", _hold(speaker, 12), occupancy=0.32),
        # Apple Vision drops the subject for two analyses, then returns.
        scenario(
            "detector_dropout",
            _hold(speaker, 3)
            + [[person("speaker", x=0.34, seen_by_ptz=False, seen_by_audience=False)]] * 2
            + _hold(speaker, 5),
            initial_pose=framed,
        ),
        # Lost to the PTZ but still visible to the wide camera: the audience
        # handoff, its direction guard, and the zoom hold.
        scenario(
            "audience_handoff",
            _hold(speaker, 3)
            + [
                [person("speaker", x=0.34 + 0.012 * index, seen_by_ptz=False)]
                for index in range(6)
            ]
            + _hold(person("speaker", x=0.412), 4),
            initial_pose=framed,
        ),
        # Detector rectangle touching the frame edge is an incomplete body.
        scenario(
            "edge_clipped",
            _hold(person("speaker", x=0.396), 10),
            initial_pose=framed,
        ),
    ]


def _record() -> dict[str, object]:
    return {
        "version": 1,
        "scenarios": {item.name: run_scenario(item).as_json() for item in _scenarios()},
    }


class SingleSubjectGoldenTests(unittest.TestCase):
    maxDiff = None

    def test_single_subject_behaviour_matches_the_recording(self) -> None:
        recorded = _record()
        if os.environ.get("PH_UPDATE_PTZ_GOLDEN"):
            GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            GOLDEN_PATH.write_text(
                json.dumps(recorded, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self.skipTest(f"Rewrote {GOLDEN_PATH.name}; review the diff before committing.")
        self.assertTrue(
            GOLDEN_PATH.exists(),
            f"{GOLDEN_PATH} is missing. Generate it with PH_UPDATE_PTZ_GOLDEN=1.",
        )
        expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            sorted(expected["scenarios"]),
            sorted(recorded["scenarios"]),
            "The recorded scenario set changed; regenerate the golden deliberately.",
        )
        for name in sorted(expected["scenarios"]):
            with self.subTest(scenario=name):
                self.assertEqual(
                    expected["scenarios"][name],
                    recorded["scenarios"][name],
                    f"Single-subject tracking changed in scenario {name!r}. If that "
                    "was intended, regenerate with PH_UPDATE_PTZ_GOLDEN=1 and say so "
                    "in the change summary; otherwise the change is mis-gated.",
                )

    def test_recording_is_reproducible(self) -> None:
        self.assertEqual(_record(), _record())


if __name__ == "__main__":
    unittest.main()
