"""A lost single subject must not be inherited by someone else.

Reproduces the two manual takeovers the stream replays caught
(testing/stage_realism/fixtures/moments): on 2026-09-20 the next reader walked
up where the last one left and received her detector ID; on 2026-09-13 the
next speaker reached the lectern after the preacher walked off the other way.
"""

from __future__ import annotations

from production_hub.tracking.association import SubjectAssociator
from production_hub.tracking.models import NormalizedRect, PersonCandidate, TrackedSubject
from production_hub.tracking.target_lock import (
    _LockedSubject,
    _ReferenceCandidate,
    _SingleSubjectMemory,
)

# Torso colour signatures: 52 bins (12 hues x 4 saturations + 4 greys).
PINK_SARI = tuple([0.0] * 44 + [0.6, 0.0, 0.0, 0.0] + [0.0, 0.0, 0.1, 0.3])
PINK_SARI_TURNING = tuple([0.0] * 44 + [0.5, 0.1, 0.0, 0.0] + [0.0, 0.0, 0.15, 0.25])
BLACK_JACKET = tuple([0.0] * 48 + [0.7, 0.2, 0.0, 0.1])


def _candidate(track: int, x: float, look=()) -> _ReferenceCandidate:
    bounds = NormalizedRect(x - 0.02, 0.05, 0.04, 0.15)
    return _ReferenceCandidate(TrackedSubject(track, bounds, 0.6, False, 5, 0.0, look), bounds)


def _lock(x: float, velocity: float, last_seen: float, look=PINK_SARI) -> _LockedSubject:
    bounds = NormalizedRect(x - 0.02, 0.05, 0.04, 0.15)
    return _LockedSubject(1, bounds, last_seen, observed_bounds=bounds, velocity_x=velocity, appearance=look)


def _departed(memory: _SingleSubjectMemory, locked: _LockedSubject, left_at: float) -> None:
    memory.observe(locked, [_candidate(1, locked.observed_bounds.center[0], locked.appearance)],
                   {1: _candidate(1, locked.observed_bounds.center[0], locked.appearance)}, left_at)
    memory.observe(locked, [], {}, left_at + 0.5)


def test_someone_seen_alongside_the_subject_never_inherits_the_lock() -> None:
    memory = _SingleSubjectMemory()
    locked = _lock(0.30, 0.0, 10.0)
    subject, next_reader = _candidate(1, 0.30, PINK_SARI), _candidate(2, 0.22, PINK_SARI)
    memory.observe(locked, [subject, next_reader], {1: subject}, 10.0)
    assert memory.rejects(locked, next_reader, 11.0)


def test_differently_dressed_person_is_not_the_lost_subject() -> None:
    memory = _SingleSubjectMemory()
    locked = _lock(0.22, -0.03, 32.4)
    _departed(memory, locked, 32.4)
    assert memory.rejects(locked, _candidate(1, 0.23, BLACK_JACKET), 60.0)
    assert not memory.rejects(locked, _candidate(1, 0.23, PINK_SARI_TURNING), 60.0)


def test_nobody_reappears_behind_where_a_walker_vanished() -> None:
    memory = _SingleSubjectMemory()
    locked = _lock(0.70, 0.08, 20.0, look=())  # walked off to the right
    _departed(memory, locked, 20.0)
    assert memory.rejects(locked, _candidate(7, 0.50, ()), 21.5)  # the lectern, behind them


def test_a_newcomer_heading_back_onto_the_stage_is_not_the_subject() -> None:
    memory = _SingleSubjectMemory()
    locked = _lock(0.22, -0.05, 5.0, look=())  # walked off to the left
    _departed(memory, locked, 5.0)
    for step, x in enumerate((0.20, 0.22, 0.24)):  # someone walking in
        now = 5.8 + step * 0.25
        entrant = _candidate(9, x, ())
        memory.observe(locked, [entrant], {}, now)
    assert memory.rejects(locked, _candidate(9, 0.25, ()), 6.4)


def test_the_subject_returning_after_a_dropout_is_accepted() -> None:
    memory = _SingleSubjectMemory()
    locked = _lock(0.40, 0.0, 3.0)
    _departed(memory, locked, 3.0)
    assert not memory.rejects(locked, _candidate(1, 0.41, PINK_SARI_TURNING), 4.0)


def test_association_does_not_hand_one_speakers_track_to_the_next() -> None:
    associator = SubjectAssociator()
    first = associator.update([PersonCandidate(NormalizedRect(0.05, 0.1, 0.25, 0.8), 0.6, PINK_SARI)],
                              observed_monotonic=1.0)
    second = associator.update([PersonCandidate(NormalizedRect(0.30, 0.1, 0.25, 0.8), 0.6, BLACK_JACKET)],
                               observed_monotonic=1.25)
    assert first[0].track_id not in {subject.track_id for subject in second if subject.last_seen_monotonic == 1.25}


def test_association_still_bridges_the_same_person() -> None:
    associator = SubjectAssociator()
    first = associator.update([PersonCandidate(NormalizedRect(0.05, 0.1, 0.25, 0.8), 0.6, PINK_SARI)],
                              observed_monotonic=1.0)
    second = associator.update([PersonCandidate(NormalizedRect(0.30, 0.1, 0.25, 0.8), 0.6, PINK_SARI_TURNING)],
                               observed_monotonic=1.25)
    assert second[0].track_id == first[0].track_id
