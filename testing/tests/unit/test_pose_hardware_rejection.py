"""Stage hardware must not become a person through a pose-only detection.

On the ICAS stage the electronic kick-drum tower produced a full skeleton
(hips, knees, feet at 0.5-0.7) with no human rectangle, and Subject Tracking
locked onto it while the preacher paced off frame. The joint confidences and
spreads below are measured values: the drum from 39 empty-stage PTZ views,
the speakers from 94 poses in our 2026-09-13/20 streams.
"""

from __future__ import annotations

from production_hub.tracking.apple_vision import merge_pose_envelopes, pose_is_person
from production_hub.tracking.models import NormalizedRect, PersonCandidate

DRUM_TOWER = {
    "right_upLeg_joint": 0.71, "left_upLeg_joint": 0.66, "root": 0.68,
    "left_leg_joint": 0.56, "right_leg_joint": 0.57, "left_foot_joint": 0.52, "right_foot_joint": 0.47,
    "right_shoulder_1_joint": 0.68, "left_shoulder_1_joint": 0.69, "neck_1_joint": 0.69,
    "head_joint": 0.54, "right_eye_joint": 0.54, "left_eye_joint": 0.43, "right_ear_joint": 0.51,
}
PREACHER = {
    "left_ear_joint": 0.69, "left_eye_joint": 0.54, "head_joint": 0.32,
    "right_shoulder_1_joint": 0.71, "left_shoulder_1_joint": 0.55, "neck_1_joint": 0.63,
    "left_forearm_joint": 0.75, "left_hand_joint": 0.80,
}


def _joints(width: float, height: float) -> dict[str, tuple[float, float]]:
    return {"a": (0.5 - width / 2, 0.3), "b": (0.5 + width / 2, 0.3 + height)}


def test_the_drum_tower_is_not_a_person() -> None:
    envelope = NormalizedRect(0.689, 0.576, 0.068, 0.321)
    assert not pose_is_person(envelope, DRUM_TOWER, _joints(0.04, 0.30))


def test_a_confident_face_on_a_pole_shaped_skeleton_is_still_rejected() -> None:
    confident = dict(DRUM_TOWER, right_eye_joint=0.9)
    assert not pose_is_person(NormalizedRect(0.6, 0.4, 0.07, 0.32), confident, _joints(0.04, 0.30))


def test_a_real_speaker_pose_stands_alone() -> None:
    assert pose_is_person(NormalizedRect(0.3, 0.1, 0.3, 0.7), PREACHER, _joints(0.26, 0.62))


def test_distant_wide_camera_poses_keep_the_lenient_path() -> None:
    # Small speakers in the church-wide image rarely show a confident face.
    assert pose_is_person(NormalizedRect(0.5, 0.2, 0.02, 0.08), DRUM_TOWER, _joints(0.01, 0.07))


def test_rejected_pose_still_enriches_a_real_rectangle_but_never_stands_alone() -> None:
    person = PersonCandidate(NormalizedRect(0.40, 0.20, 0.20, 0.50), 0.9)
    raised_hands = NormalizedRect(0.38, 0.10, 0.24, 0.60)
    drum = NormalizedRect(0.80, 0.40, 0.07, 0.32)
    for conservative in (False, True):
        merged = merge_pose_envelopes(
            [person], [raised_hands, drum], conservative=conservative, standalone=[False, False]
        )
        assert len(merged) == 1
        assert merged[0].bounds.y < person.bounds.y  # the hands still extend the box


def test_merge_without_evidence_flags_is_unchanged() -> None:
    envelope = NormalizedRect(0.4, 0.2, 0.1, 0.4)
    assert [item.bounds for item in merge_pose_envelopes([], [envelope])] == [envelope]
