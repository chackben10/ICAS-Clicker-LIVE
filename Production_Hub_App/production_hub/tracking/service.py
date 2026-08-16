from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol, Sequence

from PySide6.QtGui import QImage

from production_hub.core.config.models import CameraSceneRegion, CameraTrackingConfig
from production_hub.tracking.apple_vision import AppleVisionPersonDetector, VisionUnavailableError
from production_hub.tracking.association import SubjectAssociator
from production_hub.tracking.models import (
    NormalizedRect,
    PersonCandidate,
    TrackingSnapshot,
    TrackingState,
)
from production_hub.video.frame_broker import LatestFrameBroker
from production_hub.video.models import VideoSourceKey


class PersonDetector(Protocol):
    backend_name: str

    def detect(self, image: QImage) -> list[PersonCandidate]: ...


DetectorFactory = Callable[[CameraTrackingConfig], PersonDetector]
RegionProvider = Callable[[], Sequence[CameraSceneRegion]]


def _default_detector_factory(config: CameraTrackingConfig) -> PersonDetector:
    return AppleVisionPersonDetector(
        maximum_width=config.maximum_analysis_width,
        minimum_confidence=config.minimum_confidence,
        upper_body_only=config.upper_body_only,
        include_body_pose=config.body_pose_envelope_enabled,
        minimum_pose_joint_confidence=config.minimum_pose_joint_confidence,
    )


class PersonTrackingService:
    """Analyze only the newest frames on one worker; never issues camera commands."""

    SOURCES = (VideoSourceKey.AUDIENCE, VideoSourceKey.PTZ)

    def __init__(
        self,
        broker: LatestFrameBroker,
        config: CameraTrackingConfig,
        logger: Any | None = None,
        *,
        detector_factory: DetectorFactory = _default_detector_factory,
        initially_active: bool = True,
        region_provider: RegionProvider | None = None,
    ) -> None:
        self.broker = broker
        self.config = config
        self.logger = logger
        self.detector_factory = detector_factory
        self._region_provider = region_provider
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._activity_event = threading.Event()
        if initially_active:
            self._activity_event.set()
        self._thread: threading.Thread | None = None
        self._snapshots = {
            source: TrackingSnapshot(source=source) for source in self.SOURCES
        }
        self._associators = {
            source: self._make_associator(config) for source in self.SOURCES
        }
        self._last_sequences = {source: 0 for source in self.SOURCES}
        self._last_analysis_times = {source: 0.0 for source in self.SOURCES}
        self._rate_times = {source: deque(maxlen=60) for source in self.SOURCES}

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def active(self) -> bool:
        return self._activity_event.is_set()

    def set_active(self, active: bool) -> None:
        selected = bool(active)
        if selected == self._activity_event.is_set() and self.running:
            return
        if selected:
            self._activity_event.set()
        else:
            self._activity_event.clear()
        with self._lock:
            for source in self.SOURCES:
                snapshot = self._snapshots[source]
                if not self.config.enabled or not self._source_enabled(source):
                    snapshot.state = TrackingState.DISABLED
                    snapshot.message = "Person detection is disabled"
                elif selected:
                    snapshot.state = TrackingState.WAITING
                    snapshot.message = "Waiting for a fresh video frame"
                else:
                    self._associators[source].reset()
                    self._rate_times[source].clear()
                    snapshot.state = TrackingState.IDLE
                    snapshot.message = (
                        "Heavily throttled until person tracking is actively in use"
                    )
                    snapshot.subjects = ()
                    snapshot.analysis_fps = 0.0

    def set_region_provider(self, provider: RegionProvider | None) -> None:
        with self._lock:
            self._region_provider = provider

    def start(self) -> None:
        if not self.config.enabled or self.running:
            return
        self._stop_event.clear()
        with self._lock:
            for source in self.SOURCES:
                snapshot = self._snapshots[source]
                snapshot.state = (
                    TrackingState.WAITING
                    if self.active and self._source_enabled(source)
                    else TrackingState.IDLE
                    if self._source_enabled(source)
                    else TrackingState.DISABLED
                )
                snapshot.message = (
                    "Waiting for a fresh video frame"
                    if self.active and self._source_enabled(source)
                    else "Heavily throttled until person tracking is actively in use"
                    if self._source_enabled(source)
                    else "Detection is disabled for this source"
                )
                snapshot.last_error = ""
        self._thread = threading.Thread(
            target=self._run,
            name="production-hub-person-tracking",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        was_active = self.active
        self._stop_event.set()
        self._activity_event.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._thread = None
        if not was_active:
            self._activity_event.clear()
        with self._lock:
            for source in self.SOURCES:
                self._associators[source].reset()
                self._rate_times[source].clear()
                self._last_sequences[source] = 0
                self._last_analysis_times[source] = 0.0
                self._snapshots[source] = TrackingSnapshot(source=source)

    def reconfigure(self, config: CameraTrackingConfig) -> None:
        if self._runtime_signature(self.config) == self._runtime_signature(config):
            with self._lock:
                self.config = config
            if config.enabled and not self.running:
                self.start()
            return
        self.stop()
        with self._lock:
            self.config = config
            self._associators = {
                source: self._make_associator(config) for source in self.SOURCES
            }
        if config.enabled:
            self.start()

    def snapshot(self, source: VideoSourceKey) -> TrackingSnapshot:
        if source not in self.SOURCES:
            raise ValueError(f"Tracking is unavailable for {source.value}")
        with self._lock:
            return self._snapshots[source].copy()

    def toggle_subject(self, source: VideoSourceKey, track_id: int) -> bool:
        with self._lock:
            selected = self._associators[source].toggle(int(track_id))
            self._refresh_subject_selection(source)
            return selected

    def toggle_subject_at(self, source: VideoSourceKey, x: float, y: float) -> int | None:
        with self._lock:
            track_id = self._associators[source].toggle_at(x, y)
            self._refresh_subject_selection(source)
            return track_id

    def select_all_visible(self, source: VideoSourceKey) -> None:
        with self._lock:
            self._associators[source].select_all_visible()
            self._refresh_subject_selection(source)

    def clear_selection(self, source: VideoSourceKey | None = None) -> None:
        with self._lock:
            sources = self.SOURCES if source is None else (source,)
            for item in sources:
                self._associators[item].clear_selection()
                self._refresh_subject_selection(item)

    def _run(self) -> None:
        try:
            detector = self.detector_factory(self.config)
        except VisionUnavailableError as exc:
            self._set_all_unavailable(str(exc))
            return
        except Exception as exc:
            self._set_all_unavailable(f"Person detector could not start: {exc}")
            return

        interval = 1.0 / self.config.analysis_fps
        while not self._stop_event.is_set():
            if not self.active:
                self._activity_event.wait(timeout=1.0)
                continue
            analyzed = False
            now = time.monotonic()
            for source in self.SOURCES:
                if not self._source_enabled(source):
                    continue
                if now - self._last_analysis_times[source] < interval:
                    continue
                packet = self.broker.frame(source)
                if packet is None or packet.sequence == self._last_sequences[source]:
                    fresh = bool(
                        packet is not None
                        and now - packet.captured_monotonic < max(2.0, interval * 3.0)
                    )
                    self._set_waiting_if_needed(source, fresh)
                    continue
                self._analyze(source, packet.sequence, packet.image, detector)
                analyzed = True
                break
            if not analyzed:
                self._stop_event.wait(0.02)

    def _analyze(
        self,
        source: VideoSourceKey,
        sequence: int,
        image: QImage,
        detector: PersonDetector,
    ) -> None:
        started = time.monotonic()
        with self._lock:
            snapshot = self._snapshots[source]
            if snapshot.analyzed_frames == 0:
                snapshot.state = TrackingState.ANALYZING
                snapshot.message = "Analyzing the first frame…"
        try:
            available_regions = self._active_regions(source)
            detected = self._detect_candidates(
                source,
                image,
                detector,
                available_regions,
            )
            candidates, active_regions = self._filter_candidates(
                source,
                detected,
                available_regions=available_regions,
            )
        except Exception as exc:
            self._last_sequences[source] = sequence
            self._last_analysis_times[source] = started
            with self._lock:
                snapshot = self._snapshots[source]
                snapshot.state = TrackingState.ERROR
                snapshot.message = "Person detection failed; waiting for the next frame"
                snapshot.last_error = str(exc)
            self._log("warning", "tracking_frame_failed", str(exc), source=source.value)
            return

        completed = time.monotonic()
        with self._lock:
            subjects = self._associators[source].update(
                candidates,
                observed_monotonic=completed,
            )
            rates = self._rate_times[source]
            rates.append(completed)
            snapshot = self._snapshots[source]
            snapshot.state = TrackingState.RUNNING
            snapshot.message = f"Detected {len(subjects)} subject(s)"
            snapshot.backend = detector.backend_name
            snapshot.subjects = subjects
            snapshot.analyzed_sequence = sequence
            snapshot.analyzed_frames += 1
            snapshot.analysis_fps = self._calculate_rate(rates)
            snapshot.inference_ms = (completed - started) * 1000.0
            snapshot.raw_candidates = len(detected)
            snapshot.suppressed_candidates = len(detected) - len(candidates)
            snapshot.active_region_names = tuple(region.name for region in active_regions)
            snapshot.last_analysis_monotonic = completed
            snapshot.last_error = ""
            self._last_sequences[source] = sequence
            self._last_analysis_times[source] = started

    def _filter_candidates(
        self,
        source: VideoSourceKey,
        candidates: list[PersonCandidate],
        *,
        available_regions: tuple[CameraSceneRegion, ...] | None = None,
    ) -> tuple[list[PersonCandidate], tuple[CameraSceneRegion, ...]]:
        if source != VideoSourceKey.AUDIENCE or not self.config.audience_region_filter_enabled:
            return candidates, ()
        regions = available_regions
        if regions is None:
            # Preserve observation-only behavior for standalone services that
            # do not have Production Hub's calibrated region provider.
            return candidates, ()
        if not regions:
            return [], ()
        filtered = [
            candidate
            for candidate in candidates
            if any(
                _region_contains(
                    region,
                    candidate.bounds.x + candidate.bounds.width / 2.0,
                    min(1.0, candidate.bounds.y + candidate.bounds.height),
                )
                for region in regions
            )
        ]
        return filtered, regions

    def _active_regions(
        self,
        source: VideoSourceKey,
    ) -> tuple[CameraSceneRegion, ...] | None:
        if source != VideoSourceKey.AUDIENCE or not self.config.audience_region_filter_enabled:
            return ()
        provider = self._region_provider
        # A standalone tracking service has no live calibration authority. Keep
        # its historic observation-only behavior; VideoService always installs
        # the stabilized region provider used by Production Hub.
        if provider is None:
            return None
        try:
            available = tuple(provider())
        except Exception as exc:
            self._log("warning", "tracking_region_provider_failed", str(exc))
            return ()
        allowed_kinds = set(self.config.audience_region_kinds)
        return tuple(
            region
            for region in available
            if region.enabled
            and region.source == "audience"
            and region.kind in allowed_kinds
        )

    def _detect_candidates(
        self,
        source: VideoSourceKey,
        image: QImage,
        detector: PersonDetector,
        regions: tuple[CameraSceneRegion, ...] | None,
    ) -> list[PersonCandidate]:
        if (
            source != VideoSourceKey.AUDIENCE
            or not regions
            or not bool(getattr(detector, "supports_region_cropping", False))
        ):
            return detector.detect(image)
        candidates: list[PersonCandidate] = []
        for window in _region_detection_windows(regions):
            crop = _crop_normalized(image, window)
            if crop.isNull():
                continue
            for candidate in detector.detect(crop):
                candidates.append(
                    PersonCandidate(
                        NormalizedRect(
                            window.x + candidate.bounds.x * window.width,
                            window.y + candidate.bounds.y * window.height,
                            candidate.bounds.width * window.width,
                            candidate.bounds.height * window.height,
                        ).clamped(),
                        candidate.confidence,
                    )
                )
        return _deduplicate_candidates(candidates)

    def _source_enabled(self, source: VideoSourceKey) -> bool:
        if not self.config.enabled:
            return False
        return (
            self.config.analyze_audience
            if source == VideoSourceKey.AUDIENCE
            else self.config.analyze_ptz
        )

    def _set_waiting_if_needed(self, source: VideoSourceKey, has_fresh_frame: bool) -> None:
        with self._lock:
            snapshot = self._snapshots[source]
            if snapshot.analyzed_frames > 0 and has_fresh_frame:
                return
            if not has_fresh_frame and snapshot.state != TrackingState.WAITING:
                self._associators[source].reset()
                self._rate_times[source].clear()
                snapshot.analysis_fps = 0.0
            snapshot.state = TrackingState.WAITING
            snapshot.message = "Waiting for a fresh video frame"
            if not has_fresh_frame:
                snapshot.subjects = ()

    def _set_all_unavailable(self, message: str) -> None:
        with self._lock:
            for source in self.SOURCES:
                if self._source_enabled(source):
                    snapshot = self._snapshots[source]
                    snapshot.state = TrackingState.UNAVAILABLE
                    snapshot.message = message
                    snapshot.last_error = message
        self._log("error", "tracking_backend_unavailable", message)

    def _refresh_subject_selection(self, source: VideoSourceKey) -> None:
        self._snapshots[source].subjects = self._associators[source].visible_subjects()

    def _make_associator(self, config: CameraTrackingConfig) -> SubjectAssociator:
        return SubjectAssociator(
            minimum_iou=config.minimum_match_iou,
            maximum_center_distance=config.maximum_center_distance,
            maximum_missed_frames=config.maximum_missed_frames,
        )

    @staticmethod
    def _runtime_signature(config: CameraTrackingConfig) -> tuple[object, ...]:
        return (
            config.enabled,
            config.analyze_audience,
            config.analyze_ptz,
            config.analysis_fps,
            config.maximum_analysis_width,
            config.minimum_confidence,
            config.upper_body_only,
            config.body_pose_envelope_enabled,
            config.minimum_pose_joint_confidence,
            config.minimum_match_iou,
            config.maximum_center_distance,
            config.maximum_missed_frames,
            config.audience_region_filter_enabled,
            tuple(config.audience_region_kinds),
            tuple(
                (
                    region.id,
                    region.enabled,
                    region.kind,
                    tuple((point.x, point.y) for point in region.points),
                )
                for region in config.scene_regions
            ),
        )

    def _log(self, level: str, event: str, message: str, **metadata: object) -> None:
        if self.logger is None:
            return
        callback = getattr(self.logger, level, None)
        if callback is not None:
            callback(event, message, **metadata)

    @staticmethod
    def _calculate_rate(times: deque[float]) -> float:
        if len(times) < 2:
            return 0.0
        elapsed = times[-1] - times[0]
        return (len(times) - 1) / elapsed if elapsed > 0 else 0.0


def _region_contains(region: CameraSceneRegion, x: float, y: float) -> bool:
    points = region.points
    inside = False
    previous = points[-1]
    for current in points:
        intersects = (
            (current.y > y) != (previous.y > y)
            and x
            < (previous.x - current.x)
            * (y - current.y)
            / (previous.y - current.y)
            + current.x
        )
        if intersects:
            inside = not inside
        previous = current
    return inside


def _region_detection_windows(
    regions: Sequence[CameraSceneRegion],
    *,
    maximum_width: float = 0.30,
    overlap: float = 0.06,
    padding: float = 0.025,
) -> tuple[NormalizedRect, ...]:
    """Build a few overlapping crops around the usable stage polygons.

    The church-wide view dedicates most pixels to pews.  Cropping before the
    Vision request makes a distant speaker materially larger to the model and
    also avoids spending inference work on the audience.  Three windows cover
    the current stage while keeping CPU use bounded on the tracking worker.
    """

    points = [point for region in regions for point in region.points]
    if not points:
        return ()
    left = max(0.0, min(point.x for point in points) - padding)
    top = max(0.0, min(point.y for point in points) - padding)
    right = min(1.0, max(point.x for point in points) + padding)
    bottom = min(1.0, max(point.y for point in points) + padding)
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        return ()
    selected_width = min(maximum_width, width)
    if width <= selected_width + 1e-9:
        starts = (left,)
    else:
        stride = max(0.05, selected_width - overlap)
        count = min(3, max(2, math.ceil((width - selected_width) / stride) + 1))
        travel = width - selected_width
        starts = tuple(left + travel * index / (count - 1) for index in range(count))
    return tuple(
        NormalizedRect(start, top, selected_width, height).clamped()
        for start in starts
    )


def _crop_normalized(image: QImage, window: NormalizedRect) -> QImage:
    left = max(0, math.floor(window.x * image.width()))
    top = max(0, math.floor(window.y * image.height()))
    right = min(image.width(), math.ceil((window.x + window.width) * image.width()))
    bottom = min(image.height(), math.ceil((window.y + window.height) * image.height()))
    return image.copy(left, top, max(0, right - left), max(0, bottom - top))


def _deduplicate_candidates(
    candidates: Sequence[PersonCandidate],
) -> list[PersonCandidate]:
    selected: list[PersonCandidate] = []
    for candidate in sorted(candidates, key=lambda item: item.confidence, reverse=True):
        if any(
            candidate.bounds.intersection_over_union(existing.bounds) >= 0.30
            or candidate.bounds.center_distance(existing.bounds) <= 0.025
            for existing in selected
        ):
            continue
        selected.append(candidate)
    return sorted(selected, key=lambda item: (item.bounds.x, item.bounds.y))
