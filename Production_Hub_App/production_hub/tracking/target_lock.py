from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, Iterable

from production_hub.calibration.relocalization import RelocalizationSnapshot
from production_hub.tracking.models import (
    NormalizedRect,
    SubjectReferenceTarget,
    TrackedSubject,
    TrackingSnapshot,
)
from production_hub.tracking.ptz_geometry import (
    PtzMotorPose,
    transform_live_rect_to_reference,
)


@dataclass(frozen=True, slots=True)
class SubjectLockObservation:
    """Only the observations belonging to the subjects locked at arm time."""

    audience: TrackingSnapshot
    ptz: TrackingSnapshot
    initialized: bool
    lock_count: int
    matched_lock_count: int = 0
    audience_matches: int = 0
    audience_coasts: int = 0
    ptz_matches: int = 0
    reference_targets: tuple[SubjectReferenceTarget, ...] = ()


@dataclass(slots=True)
class _LockedSubject:
    lock_id: int
    reference_bounds: NormalizedRect
    last_seen_monotonic: float
    velocity_x: float = 0.0
    velocity_y: float = 0.0
    ptz_track_id: int | None = None
    audience_track_id: int | None = None
    pending_audience_track_id: int | None = None
    pending_audience_frames: int = 0
    audience_offset_x: float = 0.0
    audience_offset_y: float = 0.0


@dataclass(frozen=True, slots=True)
class _ReferenceCandidate:
    subject: TrackedSubject
    reference_bounds: NormalizedRect


class SubjectTargetLock:
    """Long-horizon identity lock for the operator's initial PTZ subjects.

    Apple Vision's per-camera association IDs are deliberately short lived.
    They are useful hints, but cannot define automation identity: a brief edge
    clipping can destroy one ID and a newcomer can receive the next. This lock
    is created exactly once from the stable people visible in the PTZ image
    after arming. Later observations may update those logical subjects, but
    can never add another one.
    """

    def __init__(self, *, maximum_match_distance: float = 0.30) -> None:
        self.maximum_match_distance = max(0.08, min(0.60, float(maximum_match_distance)))
        self._locks: list[_LockedSubject] = []
        self._next_lock_id = 1

    @property
    def initialized(self) -> bool:
        return bool(self._locks)

    @property
    def lock_count(self) -> int:
        return len(self._locks)

    def reset(self) -> None:
        self._locks.clear()
        self._next_lock_id = 1

    def update(
        self,
        ptz: TrackingSnapshot,
        audience: TrackingSnapshot,
        relocalization: RelocalizationSnapshot,
        geometry: Any,
        current_pose: PtzMotorPose,
        *,
        minimum_age_frames: int,
        now: float,
    ) -> SubjectLockObservation:
        ptz_candidates = self._ptz_candidates(ptz, geometry, current_pose, now)
        audience_candidates = self._audience_candidates(
            audience,
            relocalization,
            now,
        )

        if not self._locks:
            stable = [
                item
                for item in ptz_candidates
                if item.subject.age_frames >= minimum_age_frames
            ]
            # Stable left-to-right ordering gives logical lock IDs deterministic
            # meaning for diagnostics and multi-person regression tests.
            for candidate in sorted(
                stable,
                key=lambda item: (item.reference_bounds.center[0], item.subject.track_id),
            ):
                self._locks.append(
                    _LockedSubject(
                        lock_id=self._next_lock_id,
                        reference_bounds=candidate.reference_bounds,
                        last_seen_monotonic=now,
                        ptz_track_id=candidate.subject.track_id,
                    )
                )
                self._next_lock_id += 1

        if not self._locks:
            return SubjectLockObservation(
                audience=_filtered_snapshot(audience, ()),
                ptz=_filtered_snapshot(ptz, ()),
                initialized=False,
                lock_count=0,
            )

        # Match both views against the same pre-update prediction so neither
        # camera wins simply because it happened to be processed first.
        ptz_matches = self._match(ptz_candidates, source="ptz", now=now)
        audience_matches = self._match_audience(
            audience_candidates,
            ptz_matches,
            now=now,
        )

        for locked in self._locks:
            ptz_match = ptz_matches.get(locked.lock_id)
            audience_match = audience_matches.get(locked.lock_id)
            # A live PTZ observation is the composition authority. The wide
            # camera only advances the world-space estimate while the locked
            # person is outside the PTZ frame.
            selected = ptz_match or audience_match
            if selected is None:
                continue
            self._update_motion(locked, selected.reference_bounds, now)
            if ptz_match is not None:
                locked.ptz_track_id = ptz_match.subject.track_id
            if audience_match is not None:
                locked.audience_track_id = audience_match.subject.track_id

        reference_targets: list[SubjectReferenceTarget] = []
        audience_coasts = 0
        for locked in self._locks:
            ptz_match = ptz_matches.get(locked.lock_id)
            audience_match = audience_matches.get(locked.lock_id)
            selected = ptz_match or audience_match
            if selected is not None:
                reference_targets.append(
                    SubjectReferenceTarget(
                        lock_id=locked.lock_id,
                        # Drive cross-camera geometry from the logical lock's
                        # filtered world-space path, not a raw Vision box. A
                        # person detected in two overlapping Audience crops
                        # can move a few pixels between requests; at telephoto
                        # zoom that noise otherwise becomes a large motor
                        # reversal even though the identity never changed.
                        bounds=locked.reference_bounds,
                        confidence=selected.subject.confidence,
                        source="ptz" if ptz_match is not None else "audience",
                    )
                )
                continue
            coast = _audience_coast_target(locked, now)
            if coast is not None:
                reference_targets.append(coast)
                audience_coasts += 1

        return SubjectLockObservation(
            audience=_matched_snapshot(audience, audience_matches),
            ptz=_matched_snapshot(ptz, ptz_matches),
            initialized=True,
            lock_count=len(self._locks),
            matched_lock_count=(
                len(set(ptz_matches) | set(audience_matches)) + audience_coasts
            ),
            audience_matches=len(audience_matches),
            audience_coasts=audience_coasts,
            ptz_matches=len(ptz_matches),
            reference_targets=tuple(reference_targets),
        )

    def _match_audience(
        self,
        candidates: list[_ReferenceCandidate],
        ptz_matches: dict[int, _ReferenceCandidate],
        *,
        now: float,
    ) -> dict[int, _ReferenceCandidate]:
        """Return only wide-camera identities proven against the PTZ view.

        A wide church view often contains many people. A geometrically nearby
        audience detection is therefore not sufficient identity evidence. We
        bind its detector ID only after consecutive, simultaneous PTZ/wide
        observations, and never switch to a new wide ID while the subject is
        absent from the PTZ frame.
        """

        matches: dict[int, _ReferenceCandidate] = {}
        used_candidates: set[int] = set()
        for locked in self._locks:
            ptz_match = ptz_matches.get(locked.lock_id)
            remembered_index = next(
                (
                    index
                    for index, candidate in enumerate(candidates)
                    if index not in used_candidates
                    and candidate.subject.track_id == locked.audience_track_id
                ),
                None,
            )
            if remembered_index is not None:
                candidate = candidates[remembered_index]
                if ptz_match is not None:
                    if not _cross_view_compatible(ptz_match, candidate):
                        self._clear_audience_binding(locked)
                        continue
                    self._learn_audience_offset(locked, ptz_match, candidate)
                adjusted = _offset_candidate(
                    candidate,
                    locked.audience_offset_x,
                    locked.audience_offset_y,
                )
                if not _audience_motion_plausible(locked, adjusted, now):
                    # Detector IDs occasionally jump to another person after
                    # an occlusion. Treat that as a lost observation, never as
                    # permission to drive the camera across the room.
                    if ptz_match is not None:
                        self._clear_audience_binding(locked)
                    continue
                used_candidates.add(remembered_index)
                matches[locked.lock_id] = adjusted
                continue

            if ptz_match is None and locked.audience_track_id is not None:
                # Apple Vision may renew a detector ID as a person becomes
                # partially occluded at the stage edge. Continue the logical
                # lock only when the replacement follows the already-proven
                # wide-camera motion path for two consecutive observations.
                predicted = _predicted_bounds(locked, now)
                compatible_replacements: list[
                    tuple[int, _ReferenceCandidate]
                ] = []
                for index, candidate in enumerate(candidates):
                    if index in used_candidates:
                        continue
                    adjusted = _offset_candidate(
                        candidate,
                        locked.audience_offset_x,
                        locked.audience_offset_y,
                    )
                    if not _audience_motion_plausible(locked, adjusted, now):
                        continue
                    if _area_log_error(
                        predicted,
                        adjusted.reference_bounds,
                    ) > 1.0:
                        continue
                    compatible_replacements.append((index, adjusted))
                if not compatible_replacements:
                    locked.pending_audience_track_id = None
                    locked.pending_audience_frames = 0
                    continue
                candidate_index, adjusted = min(
                    compatible_replacements,
                    key=lambda item: _anchor_distance(
                        predicted,
                        item[1].reference_bounds,
                    ),
                )
                replacement_id = adjusted.subject.track_id
                if replacement_id == locked.pending_audience_track_id:
                    locked.pending_audience_frames += 1
                else:
                    locked.pending_audience_track_id = replacement_id
                    locked.pending_audience_frames = 1
                if locked.pending_audience_frames < 2:
                    continue
                locked.audience_track_id = replacement_id
                locked.pending_audience_track_id = None
                locked.pending_audience_frames = 0
                used_candidates.add(candidate_index)
                matches[locked.lock_id] = adjusted
                continue

            # A new or replacement Audience ID can only be established while
            # the same logical subject is simultaneously visible in the PTZ.
            if ptz_match is None:
                continue
            compatible = [
                (index, candidate)
                for index, candidate in enumerate(candidates)
                if index not in used_candidates
                and _cross_view_compatible(ptz_match, candidate)
            ]
            if not compatible:
                locked.pending_audience_track_id = None
                locked.pending_audience_frames = 0
                continue
            candidate_index, candidate = min(
                compatible,
                key=lambda item: _anchor_distance(
                    ptz_match.reference_bounds,
                    item[1].reference_bounds,
                ),
            )
            if candidate.subject.track_id == locked.pending_audience_track_id:
                locked.pending_audience_frames += 1
            else:
                locked.pending_audience_track_id = candidate.subject.track_id
                locked.pending_audience_frames = 1
            self._learn_audience_offset(locked, ptz_match, candidate)
            if locked.pending_audience_frames < 2:
                continue
            locked.audience_track_id = candidate.subject.track_id
            locked.pending_audience_track_id = None
            locked.pending_audience_frames = 0
            used_candidates.add(candidate_index)
            matches[locked.lock_id] = _offset_candidate(
                candidate,
                locked.audience_offset_x,
                locked.audience_offset_y,
            )
        return matches

    @staticmethod
    def _clear_audience_binding(locked: _LockedSubject) -> None:
        locked.audience_track_id = None
        locked.pending_audience_track_id = None
        locked.pending_audience_frames = 0
        locked.audience_offset_x = 0.0
        locked.audience_offset_y = 0.0

    @staticmethod
    def _learn_audience_offset(
        locked: _LockedSubject,
        ptz: _ReferenceCandidate,
        audience: _ReferenceCandidate,
    ) -> None:
        ptz_x, ptz_y = ptz.reference_bounds.center
        audience_x, audience_y = audience.reference_bounds.center
        measured_x = ptz_x - audience_x
        measured_y = ptz_y - audience_y
        alpha = 1.0 if locked.audience_track_id is None else 0.18
        locked.audience_offset_x += (
            measured_x - locked.audience_offset_x
        ) * alpha
        locked.audience_offset_y += (
            measured_y - locked.audience_offset_y
        ) * alpha

    def _match(
        self,
        candidates: list[_ReferenceCandidate],
        *,
        source: str,
        now: float,
    ) -> dict[int, _ReferenceCandidate]:
        pairs: list[tuple[float, int, int]] = []
        single_lock = len(self._locks) == 1
        for locked in self._locks:
            predicted = _predicted_bounds(locked, now)
            elapsed = max(0.0, now - locked.last_seen_monotonic)
            remembered_id = (
                locked.ptz_track_id if source == "ptz" else locked.audience_track_id
            )
            for candidate_index, candidate in enumerate(candidates):
                distance = min(
                    predicted.center_distance(candidate.reference_bounds),
                    locked.reference_bounds.center_distance(candidate.reference_bounds),
                )
                same_source_id = candidate.subject.track_id == remembered_id
                # Grow the reacquisition gate slowly through detector dropouts,
                # while retaining a finite bound that rejects unrelated people.
                maximum = min(
                    0.55 if single_lock else 0.42,
                    self.maximum_match_distance + elapsed * 0.018,
                )
                if same_source_id:
                    maximum = min(0.60, maximum + 0.10)
                iou = predicted.intersection_over_union(candidate.reference_bounds)
                if distance > maximum and iou < 0.03:
                    continue
                size_error = abs(
                    math.log(
                        max(candidate.reference_bounds.area, 1e-6)
                        / max(predicted.area, 1e-6)
                    )
                )
                score = (
                    distance
                    + min(1.5, size_error) * 0.035
                    - iou * 0.10
                    - (0.10 if same_source_id else 0.0)
                )
                pairs.append((score, locked.lock_id, candidate_index))

        selected_locks: set[int] = set()
        selected_candidates: set[int] = set()
        matches: dict[int, _ReferenceCandidate] = {}
        for _score, lock_id, candidate_index in sorted(pairs):
            if lock_id in selected_locks or candidate_index in selected_candidates:
                continue
            selected_locks.add(lock_id)
            selected_candidates.add(candidate_index)
            matches[lock_id] = candidates[candidate_index]
        return matches

    @staticmethod
    def _update_motion(
        locked: _LockedSubject,
        observed: NormalizedRect,
        now: float,
    ) -> None:
        elapsed = max(1e-3, now - locked.last_seen_monotonic)
        old_x, old_y = locked.reference_bounds.center
        new_x, new_y = observed.center
        measured_x = max(-0.20, min(0.20, (new_x - old_x) / elapsed))
        measured_y = max(-0.20, min(0.20, (new_y - old_y) / elapsed))
        alpha = 0.35
        locked.velocity_x += (measured_x - locked.velocity_x) * alpha
        locked.velocity_y += (measured_y - locked.velocity_y) * alpha
        locked.reference_bounds = _smooth_reference_bounds(
            locked.reference_bounds,
            observed,
        )
        locked.last_seen_monotonic = now

    @staticmethod
    def _ptz_candidates(
        snapshot: TrackingSnapshot,
        geometry: Any,
        current_pose: PtzMotorPose,
        now: float,
    ) -> list[_ReferenceCandidate]:
        candidates: list[_ReferenceCandidate] = []
        if snapshot.last_analysis_monotonic <= 0 or now - snapshot.last_analysis_monotonic > 2.0:
            return candidates
        for subject in _currently_observed(
            snapshot.subjects,
            snapshot.last_analysis_monotonic,
        ):
            try:
                bounds = geometry.ptz_rect_to_reference(subject.bounds, current_pose)
            except ValueError:
                continue
            candidates.append(_ReferenceCandidate(subject, bounds))
        return candidates

    @staticmethod
    def _audience_candidates(
        snapshot: TrackingSnapshot,
        relocalization: RelocalizationSnapshot,
        now: float,
    ) -> list[_ReferenceCandidate]:
        candidates: list[_ReferenceCandidate] = []
        if (
            not relocalization.motion_safe
            or snapshot.last_analysis_monotonic <= 0
            or now - snapshot.last_analysis_monotonic > 2.0
        ):
            return candidates
        for subject in _currently_observed(
            snapshot.subjects,
            snapshot.last_analysis_monotonic,
        ):
            try:
                bounds = transform_live_rect_to_reference(
                    subject.bounds,
                    relocalization.live_to_reference,
                    relocalization.live_size,
                    relocalization.reference_size,
                )
            except ValueError:
                continue
            candidates.append(_ReferenceCandidate(subject, bounds))
        return candidates


def _currently_observed(
    subjects: Iterable[TrackedSubject],
    analysis_monotonic: float,
) -> tuple[TrackedSubject, ...]:
    # SubjectAssociator retains missed rectangles briefly. Their timestamp is
    # unchanged, so exclude them here whenever a fresh detector update exists.
    values = tuple(subjects)
    if not values:
        return ()
    observed_at = float(analysis_monotonic or 0.0)
    if observed_at <= 0:
        observed_at = max(item.last_seen_monotonic for item in values)
    return tuple(
        item
        for item in values
        if observed_at - item.last_seen_monotonic <= 0.08
    )


def _predicted_bounds(locked: _LockedSubject, now: float) -> NormalizedRect:
    elapsed = min(2.0, max(0.0, now - locked.last_seen_monotonic))
    return NormalizedRect(
        locked.reference_bounds.x + locked.velocity_x * elapsed,
        locked.reference_bounds.y + locked.velocity_y * elapsed,
        locked.reference_bounds.width,
        locked.reference_bounds.height,
    ).clamped()


def _audience_coast_target(
    locked: _LockedSubject,
    now: float,
) -> SubjectReferenceTarget | None:
    """Briefly coast a proven wide-camera identity through detector misses.

    Apple Vision regularly misses a distant walking person for one to several
    wide-camera frames. A short, velocity-limited prediction is safer than
    stopping at the PTZ edge: it gives the camera time to bring that same
    person back into its authoritative close view. The prediction is only
    available after a simultaneous PTZ/Audience observation established the
    cross-camera identity, and it expires before it can wander across the
    room.
    """

    elapsed = max(0.0, now - locked.last_seen_monotonic)
    if locked.audience_track_id is None or elapsed > 1.5:
        return None
    speed = math.hypot(locked.velocity_x, locked.velocity_y)
    if speed < 0.004:
        return None
    return SubjectReferenceTarget(
        lock_id=locked.lock_id,
        bounds=_predicted_bounds(locked, now),
        confidence=max(0.25, 0.55 - elapsed * 0.16),
        source="audience",
    )


def _subject_anchor(bounds: NormalizedRect) -> tuple[float, float]:
    """Use the lower body as the most viewpoint-stable person landmark."""

    return (
        bounds.x + bounds.width / 2.0,
        bounds.y + bounds.height * 0.88,
    )


def _anchor_distance(left: NormalizedRect, right: NormalizedRect) -> float:
    left_x, left_y = _subject_anchor(left)
    right_x, right_y = _subject_anchor(right)
    return math.hypot(left_x - right_x, left_y - right_y)


def _cross_view_compatible(
    ptz: _ReferenceCandidate,
    audience: _ReferenceCandidate,
) -> bool:
    # Both candidates have already been projected into the same calibrated
    # reference canvas. The lower-body gate accommodates modest parallax while
    # rejecting congregation detections elsewhere in the wide frame.
    return _anchor_distance(ptz.reference_bounds, audience.reference_bounds) <= 0.13


def _offset_candidate(
    candidate: _ReferenceCandidate,
    offset_x: float,
    offset_y: float,
) -> _ReferenceCandidate:
    bounds = candidate.reference_bounds
    return _ReferenceCandidate(
        candidate.subject,
        NormalizedRect(
            bounds.x + offset_x,
            bounds.y + offset_y,
            bounds.width,
            bounds.height,
        ),
    )


def _audience_motion_plausible(
    locked: _LockedSubject,
    candidate: _ReferenceCandidate,
    now: float,
) -> bool:
    elapsed = max(0.0, now - locked.last_seen_monotonic)
    predicted = _predicted_bounds(locked, now)
    # At the normal 4 fps cadence this admits purposeful walking but rejects
    # the large single-frame jumps produced when a wide-view detector ID is
    # reassigned to somebody in the congregation.
    maximum = min(0.18, 0.045 + elapsed * 0.10)
    return _anchor_distance(predicted, candidate.reference_bounds) <= maximum


def _area_log_error(left: NormalizedRect, right: NormalizedRect) -> float:
    return abs(
        math.log(max(right.area, 1e-6) / max(left.area, 1e-6))
    )


def _smooth_reference_bounds(
    previous: NormalizedRect,
    observed: NormalizedRect,
) -> NormalizedRect:
    # Matching needs responsiveness to a walking subject, but a modest size
    # filter prevents body-pose envelope changes from looking like teleporting.
    alpha = 0.62
    return NormalizedRect(
        previous.x + (observed.x - previous.x) * alpha,
        previous.y + (observed.y - previous.y) * alpha,
        previous.width + (observed.width - previous.width) * 0.35,
        previous.height + (observed.height - previous.height) * 0.35,
    ).clamped()


def _filtered_snapshot(
    snapshot: TrackingSnapshot,
    subjects: Iterable[TrackedSubject],
) -> TrackingSnapshot:
    return replace(snapshot, subjects=tuple(subjects))


def _matched_snapshot(
    snapshot: TrackingSnapshot,
    matches: dict[int, _ReferenceCandidate],
) -> TrackingSnapshot:
    subjects = [
        replace(candidate.subject, track_id=lock_id, selected=True)
        for lock_id, candidate in sorted(matches.items())
    ]
    return _filtered_snapshot(snapshot, subjects)
