"""What makes a tracked shot pleasant to watch, not only correct (Dev experiments).

Pins the pieces tuned against the stage-realism watchability metrics
(testing/stage_realism/scoring.py `watchability`): hold still while a standing
subject is framed, breathe out to a planned medium shot and hold it until they
settle, keep the headroom while breathed out, keep a medium shot in front of
the stage, cap the picture speed, and two identity guards found on the way.
"""

from __future__ import annotations

from types import SimpleNamespace

from production_hub.integrations.panasonic_awp import speed_model
from production_hub.tracking import velocity_controller as vc
from production_hub.tracking.models import NormalizedRect, TrackedSubject
from production_hub.tracking.ptz_automation import _feet_in_front_of_stage
from production_hub.tracking.ptz_geometry import PtzMotorPose
from production_hub.tracking.target_lock import (
    _audience_motion_plausible,
    _LockedSubject,
    _ReferenceCandidate,
    _SingleSubjectMemory,
)


def _config():
    return SimpleNamespace(
        decision_fps=4.0, continuous_error_smoothing=0.58, continuous_velocity_smoothing=0.45,
        continuous_prediction_seconds=0.55, subject_center_deadband_x=0.06, subject_center_deadband_y=0.09,
        continuous_pan_max_speed=26, continuous_tilt_max_speed=14, continuous_zoom_max_speed=8,
        continuous_speed_slew_per_second=18.0, pan_deadband_units=80, tilt_deadband_units=60,
        zoom_deadband_units=35, continuous_zoom_dwell_seconds=0.6, group_zoom_out_max_speed=20,
    )


def _candidate(track: int, x: float) -> _ReferenceCandidate:
    bounds = NormalizedRect(x - 0.02, 0.05, 0.04, 0.15)
    return _ReferenceCandidate(TrackedSubject(track, bounds, 0.6, False, 5, 0.0, ()), bounds)


# --- identity ----------------------------------------------------------------


def test_one_missed_frame_is_not_a_licence_to_jump_to_someone_else() -> None:
    # A wider shot brought the next speaker in just as the subject blinked out
    # of detection: 0.17 away a quarter of a second later is not the subject.
    bounds = NormalizedRect(0.22, 0.05, 0.04, 0.15)
    locked = _LockedSubject(1, bounds, 10.0, observed_bounds=bounds, velocity_x=0.0)
    assert not _audience_motion_plausible(locked, _candidate(7, 0.41), 10.25, single=True)
    assert _audience_motion_plausible(locked, _candidate(1, 0.255), 10.25, single=True)


def test_a_wide_camera_id_seen_alongside_the_subject_is_someone_else() -> None:
    # The audience detector gave the walking reader a new ID and handed her
    # old one to the next speaker; both were visible together for a while.
    memory = _SingleSubjectMemory()
    reader, next_speaker = _candidate(19, 0.29), _candidate(1, 0.38)
    duplicate = _ReferenceCandidate(
        TrackedSubject(20, reader.reference_bounds, 0.5, False, 5, 0.0, ()), reader.reference_bounds,
    )
    memory.note_audience(19, [reader, next_speaker, duplicate], 10.5)
    assert memory.audience_companion(1, 13.0)
    assert not memory.audience_companion(20, 13.0)  # a second box on the reader herself
    assert not memory.audience_companion(1, 10.5 + 21.0)  # forgotten eventually


# --- staying put -------------------------------------------------------------


def test_a_framed_standing_subject_holds_until_a_clear_drift() -> None:
    assert vc._framed(False, 0.04, 0.0)
    assert not vc._framed(False, 0.04, 5.0)  # still moving: not settled yet
    assert vc._framed(True, 0.12, 0.0)  # a sway or small step: hold
    assert not vc._framed(True, 0.16, 0.0)  # a clear drift: re-frame


def test_walk_evidence_is_measured_in_time_not_updates() -> None:
    slow, fast = vc._WalkEvidence(), vc._WalkEvidence()
    for step in range(3):
        slow.observe(-600.0, step * 0.25)
    for step in range(5):
        fast.observe(-600.0, step * 0.125)
    assert slow.streak == fast.streak == 3.0


# --- zoom --------------------------------------------------------------------


def test_an_escape_breathes_out_to_the_medium_shot_then_stops() -> None:
    controller = vc.SmoothPtzVelocityController()
    at_edge = NormalizedRect(0.80, 0.12, 0.10, 0.60)
    assert controller._escape_through(0.8, PtzMotorPose(43000, 33300, 3500), at_edge) == 0.8
    # The trigger has passed but the shot is still tight: keep breathing out.
    assert controller._escape_through(0.0, PtzMotorPose(43000, 33300, 3300), at_edge) == vc.ESCAPE_THROUGH_SPEED
    assert controller._escape_through(0.0, PtzMotorPose(43000, 33300, 2890), at_edge) == 0.0
    assert not controller._escape_episode


def test_the_shot_does_not_push_back_in_until_they_have_stood_still() -> None:
    controller = vc.SmoothPtzVelocityController()
    config = _config()
    centred = NormalizedRect(0.45, 0.12, 0.10, 0.60)
    controller._has_moved = True
    tight = PtzMotorPose(43000, 33300, 3500)
    wide = PtzMotorPose(43000, 33300, 2900)
    zooms = []
    for step in range(20):
        command = controller.update(
            actual=wide, desired=tight, config=config, now=step * 0.25, live_bounds=centred,
            ease_motion=True, responsive_tracking=True, safe_zone_control=True,
        )
        zooms.append(command.zoom)
    settle_steps = int(vc.ZOOM_IN_SETTLE_SECONDS / 0.25)
    assert all(zoom == 50 for zoom in zooms[:settle_steps])
    assert zooms[-1] > 50  # then pushes in


def test_an_escape_zoom_out_stops_promptly_when_it_ends() -> None:
    controller = vc.SmoothPtzVelocityController()
    controller._zoom_delta = -vc.ESCAPE_ZOOM_SPEED
    controller._last_update = 0.0
    command = controller.update(
        actual=PtzMotorPose(43000, 33300, 2950), desired=PtzMotorPose(43000, 33300, 2950), config=_config(),
        now=0.25, live_bounds=NormalizedRect(0.45, 0.12, 0.10, 0.60),
        ease_motion=True, responsive_tracking=True, safe_zone_control=True,
    )
    assert abs(command.zoom - 50) <= 30  # not still drifting wider at escape speed


def test_breathing_out_tilts_to_keep_the_headroom() -> None:
    controller = vc.SmoothPtzVelocityController()
    controller._headroom_ref = (0.12, 3500.0, 33300.0)
    controller._escape_episode = True
    # Zoomed out from 3500 to 2900 about the frame centre: the subject has sunk.
    target = controller._keep_headroom(0.0, PtzMotorPose(43000, 33300, 2900), _config())
    assert target < 0.0  # tilt down (Panasonic below 50) so the head rises back
    # A head nearing the top edge still wins.
    assert controller._keep_headroom(12.0, PtzMotorPose(43000, 33300, 2900), _config()) == 12.0


def test_front_of_stage_means_off_the_deck() -> None:
    def region(kind, left, top, right, bottom):
        corners = [(left, top), (right, top), (right, bottom), (left, bottom)]
        return SimpleNamespace(
            enabled=True, source="audience", kind=kind,
            points=[SimpleNamespace(x=x, y=y) for x, y in corners],
        )

    regions = [region("stage", 0.30, 0.00, 0.70, 0.20), region("front_stage", 0.30, 0.15, 0.70, 0.35)]
    on_the_floor = NormalizedRect(0.48, 0.12, 0.04, 0.13)  # feet at 0.25
    at_the_front_of_the_deck = NormalizedRect(0.48, 0.05, 0.04, 0.12)  # feet at 0.17: in both
    assert _feet_in_front_of_stage(regions, on_the_floor)
    assert not _feet_in_front_of_stage(regions, at_the_front_of_the_deck)


# --- speed -------------------------------------------------------------------


def test_picture_speed_is_the_same_motion_at_any_zoom() -> None:
    assert speed_model.frame_units("pan", 2900) > speed_model.frame_units("pan", 3500)
    for zoom in (2400, 2900, 3500):
        offset = speed_model.offset_for_picture_speed("pan", 0.4, zoom)
        assert abs(speed_model.picture_speed("pan", offset, zoom) - 0.4) < 0.01


def test_a_standing_subject_is_never_whipped_across_at_a_medium_shot() -> None:
    controller = vc.SmoothPtzVelocityController()
    zoom = 2839
    fastest = speed_model.offset_for_picture_speed("pan", vc.MAX_PICTURE_SPEED, zoom)
    # Well inside the frame (not half out), far off centre, not walking.
    bounds = NormalizedRect(0.14, 0.12, 0.10, 0.60)
    for step in range(12):
        command = controller.update(
            actual=PtzMotorPose(43000, 33300, zoom), desired=PtzMotorPose(43000, 33300, zoom),
            config=_config(), now=step * 0.25, live_bounds=bounds,
            ease_motion=True, responsive_tracking=True, safe_zone_control=True,
        )
        assert abs(command.pan - 50) <= round(fastest) + 1


def test_dev_decides_once_per_detection() -> None:
    from production_hub.tracking.ptz_automation import PtzAutomationService

    service = PtzAutomationService.__new__(PtzAutomationService)
    service.config = SimpleNamespace(analysis_fps=8.0, automation=SimpleNamespace(decision_fps=4.0))
    service.tracking_refinements_enabled = False
    assert service.decision_rate() == 4.0  # live behaviour: the configured rate
    service.tracking_refinements_enabled = True
    assert service.decision_rate() == 8.0
    service.config.analysis_fps = 2.0
    assert service.decision_rate() == 4.0  # never slower than configured
    service.config.analysis_fps = 12.0
    assert service.decision_rate() == 8.0  # the decision rate's own limit


def test_an_id_that_reverses_at_once_with_someone_nearby_moved_to_them() -> None:
    # The reader walks left; the detector hands her ID to a look-alike walking
    # in the other way. People slow before they turn back.
    memory = _SingleSubjectMemory()
    for step, x in enumerate((0.40, 0.385, 0.37, 0.355)):
        memory.note_walk("ptz", _candidate(1, x), step * 0.125)
    swapped = _candidate(1, 0.37)
    assert not memory.instant_reversal("ptz", swapped, 0.5)  # nobody else about: PTZ mapping jitter
    reader, twin = _candidate(4, 0.35), _candidate(3, 0.40)
    memory.note_audience(4, [reader, twin], 0.4)
    assert memory.instant_reversal("ptz", swapped, 0.5)
    assert not memory.instant_reversal("ptz", _candidate(1, 0.353), 0.5)  # a real, slow turn


def test_a_nearer_wide_detection_to_where_the_ptz_has_the_subject_wins() -> None:
    from production_hub.tracking.target_lock import _closer_detection_elsewhere

    bounds = NormalizedRect(0.26, 0.05, 0.04, 0.15)
    locked = _LockedSubject(1, bounds, 8.0, observed_bounds=bounds, velocity_x=-0.05)
    ptz = _candidate(2, 0.28)
    remembered, reader = _candidate(3, 0.37), _candidate(4, 0.29)
    assert _closer_detection_elsewhere(locked, ptz, remembered, [remembered, reader], set()) == 1
    assert _closer_detection_elsewhere(locked, ptz, _candidate(3, 0.30), [_candidate(3, 0.30), reader], set()) is None
