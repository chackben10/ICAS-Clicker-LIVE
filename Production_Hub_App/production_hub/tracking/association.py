from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

from production_hub.tracking.models import NormalizedRect, PersonCandidate, TrackedSubject


@dataclass(slots=True)
class _Track:
    track_id: int
    bounds: NormalizedRect
    confidence: float
    first_seen_monotonic: float
    last_seen_monotonic: float
    age_frames: int = 1
    missed_frames: int = 0


class SubjectAssociator:
    """Deterministic short-horizon identity association for detector observations."""

    def __init__(
        self,
        *,
        minimum_iou: float = 0.12,
        maximum_center_distance: float = 0.18,
        maximum_missed_frames: int = 4,
        retain_missed_with_active: bool = True,
    ) -> None:
        self.minimum_iou = max(0.0, min(1.0, float(minimum_iou)))
        self.maximum_center_distance = max(0.01, min(1.0, float(maximum_center_distance)))
        self.maximum_missed_frames = max(0, int(maximum_missed_frames))
        self.retain_missed_with_active = bool(retain_missed_with_active)
        self._tracks: dict[int, _Track] = {}
        self._selected_ids: set[int] = set()
        self._dormant: dict[int, _Track] = {}
        self._next_id = 1

    @property
    def selected_ids(self) -> frozenset[int]:
        return frozenset(self._selected_ids)

    def update(
        self,
        candidates: list[PersonCandidate],
        *,
        observed_monotonic: float | None = None,
    ) -> tuple[TrackedSubject, ...]:
        observed_at = float(monotonic() if observed_monotonic is None else observed_monotonic)
        self._dormant = {key: track for key, track in self._dormant.items()
                         if 0 <= observed_at - track.last_seen_monotonic <= 2.0}
        normalized = [
            PersonCandidate(item.bounds.clamped(), max(0.0, min(1.0, item.confidence)))
            for item in candidates
            if item.bounds.clamped().area > 0
        ]
        pair_scores: list[tuple[float, int, int]] = []
        single_subject_handoff = len(self._tracks) == 1 and len(normalized) == 1
        for track_id, track in self._tracks.items():
            for candidate_index, candidate in enumerate(normalized):
                iou = track.bounds.intersection_over_union(candidate.bounds)
                distance = track.bounds.center_distance(candidate.bounds)
                maximum_distance = (
                    max(self.maximum_center_distance, 0.36)
                    if single_subject_handoff
                    else self.maximum_center_distance
                )
                if iou < self.minimum_iou and distance > maximum_distance:
                    continue
                proximity = max(0.0, 1.0 - distance / maximum_distance)
                pair_scores.append((iou * 0.8 + proximity * 0.2, track_id, candidate_index))

        matched_tracks: set[int] = set()
        matched_candidates: set[int] = set()
        for _score, track_id, candidate_index in sorted(pair_scores, reverse=True):
            if track_id in matched_tracks or candidate_index in matched_candidates:
                continue
            track = self._tracks[track_id]
            candidate = normalized[candidate_index]
            track.bounds = _smoothed_bounds(track.bounds, candidate.bounds)
            track.confidence = _lerp(track.confidence, candidate.confidence, 0.45)
            track.last_seen_monotonic = observed_at
            track.age_frames += 1
            track.missed_frames = 0
            matched_tracks.add(track_id)
            matched_candidates.add(candidate_index)

        for track_id, track in list(self._tracks.items()):
            if track_id not in matched_tracks:
                track.missed_frames += 1
                if track.missed_frames > self.maximum_missed_frames:
                    if 0 <= observed_at - track.last_seen_monotonic <= 2.0:
                        self._dormant[track_id] = track
                    self._tracks.pop(track_id, None)
                    self._selected_ids.discard(track_id)

        for candidate_index, candidate in enumerate(normalized):
            if candidate_index in matched_candidates:
                continue
            # Hidden identity memory is not a visible/coasted subject and is
            # never selected for steering. Reuse an ID only for an unambiguous,
            # nearby return with compatible size; active matches take priority.
            possible = []
            for dormant_id, old in self._dormant.items():
                ratio = candidate.bounds.area / max(old.bounds.area, 1e-9)
                overlap = old.bounds.intersection_over_union(candidate.bounds)
                if (1/3 <= ratio <= 3 and overlap >= .35
                        and old.bounds.center_distance(candidate.bounds) <= .08):
                    possible.append((overlap, dormant_id))
            possible.sort(reverse=True)
            if possible and (len(possible) == 1 or possible[0][0] - possible[1][0] >= .15):
                track_id = possible[0][1]
                self._dormant.pop(track_id)
            else:
                track_id = self._next_id
                self._next_id += 1
            self._tracks[track_id] = _Track(
                track_id=track_id,
                bounds=candidate.bounds,
                confidence=candidate.confidence,
                first_seen_monotonic=observed_at,
                last_seen_monotonic=observed_at,
            )

        return self.visible_subjects()

    def visible_subjects(self) -> tuple[TrackedSubject, ...]:
        has_active_track = any(track.missed_frames == 0 for track in self._tracks.values())
        subjects = [
            TrackedSubject(
                track_id=track.track_id,
                bounds=track.bounds,
                confidence=track.confidence,
                selected=track.track_id in self._selected_ids,
                age_frames=track.age_frames,
                last_seen_monotonic=track.last_seen_monotonic,
            )
            for track in self._tracks.values()
            # Apple Vision can omit an otherwise stable person for an isolated
            # frame, especially during gestures or motion blur. Keep the last
            # observation visible for the configured short association horizon
            # so framing does not drop and reacquire the same speaker.
            if track.missed_frames <= self.maximum_missed_frames
            and (
                self.retain_missed_with_active
                or not has_active_track
                or track.missed_frames == 0
            )
        ]
        return tuple(sorted(subjects, key=lambda item: (item.bounds.x, item.track_id)))

    def toggle(self, track_id: int) -> bool:
        if track_id not in self._tracks:
            return False
        if track_id in self._selected_ids:
            self._selected_ids.remove(track_id)
            return False
        self._selected_ids.add(track_id)
        return True

    def toggle_at(self, x: float, y: float) -> int | None:
        matches = [
            track
            for track in self._tracks.values()
            if track.missed_frames == 0 and track.bounds.contains(float(x), float(y))
        ]
        if not matches:
            return None
        chosen = min(matches, key=lambda item: (item.bounds.area, -item.confidence))
        self.toggle(chosen.track_id)
        return chosen.track_id

    def select_all_visible(self) -> None:
        self._selected_ids.update(
            track.track_id for track in self._tracks.values() if track.missed_frames == 0
        )

    def clear_selection(self) -> None:
        self._selected_ids.clear()

    def reset(self) -> None:
        self._tracks.clear()
        self._dormant.clear()
        self._selected_ids.clear()
        self._next_id = 1


def _lerp(start: float, end: float, alpha: float) -> float:
    return start + (end - start) * alpha


def _smoothed_bounds(previous: NormalizedRect, observed: NormalizedRect) -> NormalizedRect:
    """Suppress detector noise while remaining responsive to deliberate motion."""

    center_motion = previous.center_distance(observed)
    size_motion = max(
        abs(previous.width - observed.width),
        abs(previous.height - observed.height),
    )
    alpha = min(
        0.82,
        max(0.42, 0.42 + center_motion * 4.0 + size_motion * 1.5),
    )
    return NormalizedRect(
        _lerp(previous.x, observed.x, alpha),
        _lerp(previous.y, observed.y, alpha),
        _lerp(previous.width, observed.width, alpha),
        _lerp(previous.height, observed.height, alpha),
    ).clamped()
