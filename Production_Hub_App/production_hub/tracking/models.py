from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import StrEnum
from time import monotonic

from production_hub.video.models import VideoSourceKey


class TrackingState(StrEnum):
    DISABLED = "disabled"
    IDLE = "idle"
    WAITING = "waiting"
    ANALYZING = "analyzing"
    RUNNING = "running"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class NormalizedRect:
    """Top-left-origin rectangle whose values are normalized to the video frame."""

    x: float
    y: float
    width: float
    height: float

    def clamped(self) -> NormalizedRect:
        left = max(0.0, min(1.0, float(self.x)))
        top = max(0.0, min(1.0, float(self.y)))
        right = max(left, min(1.0, float(self.x + self.width)))
        bottom = max(top, min(1.0, float(self.y + self.height)))
        return NormalizedRect(left, top, right - left, bottom - top)

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2.0, self.y + self.height / 2.0

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x <= self.x + self.width and self.y <= y <= self.y + self.height

    def intersection_over_union(self, other: NormalizedRect) -> float:
        left = max(self.x, other.x)
        top = max(self.y, other.y)
        right = min(self.x + self.width, other.x + other.width)
        bottom = min(self.y + self.height, other.y + other.height)
        intersection = max(0.0, right - left) * max(0.0, bottom - top)
        union = self.area + other.area - intersection
        return intersection / union if union > 0 else 0.0

    def center_distance(self, other: NormalizedRect) -> float:
        own_x, own_y = self.center
        other_x, other_y = other.center
        return math.hypot(own_x - other_x, own_y - other_y)


@dataclass(frozen=True, slots=True)
class PersonCandidate:
    bounds: NormalizedRect
    confidence: float


@dataclass(frozen=True, slots=True)
class TrackedSubject:
    track_id: int
    bounds: NormalizedRect
    confidence: float
    selected: bool
    age_frames: int
    last_seen_monotonic: float


@dataclass(slots=True)
class TrackingSnapshot:
    source: VideoSourceKey
    state: TrackingState = TrackingState.DISABLED
    message: str = "Person detection is disabled"
    backend: str = "Apple Vision"
    subjects: tuple[TrackedSubject, ...] = ()
    analyzed_sequence: int = 0
    analyzed_frames: int = 0
    analysis_fps: float = 0.0
    inference_ms: float = 0.0
    raw_candidates: int = 0
    suppressed_candidates: int = 0
    active_region_names: tuple[str, ...] = ()
    last_analysis_monotonic: float = 0.0
    last_error: str = ""

    @property
    def analysis_age_seconds(self) -> float | None:
        if self.last_analysis_monotonic <= 0:
            return None
        return max(0.0, monotonic() - self.last_analysis_monotonic)

    @property
    def selected_count(self) -> int:
        return sum(subject.selected for subject in self.subjects)

    def copy(self) -> TrackingSnapshot:
        return replace(
            self,
            subjects=tuple(self.subjects),
            active_region_names=tuple(self.active_region_names),
        )
