from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PySide6.QtGui import QImage

from production_hub.calibration.review import (
    CalibrationReviewData,
    load_active_calibration_review,
)
from production_hub.core.config.models import CameraSceneRegion, CameraTrackingConfig
from production_hub.tracking.scene_regions import transform_scene_regions
from production_hub.video.frame_broker import LatestFrameBroker
from production_hub.video.models import VideoSourceKey


class RelocalizationState(StrEnum):
    DISABLED = "disabled"
    IDLE = "idle"
    WAITING = "waiting"
    LOCKING = "locking"
    LOCKED = "locked"
    DEGRADED = "degraded"
    LOST = "lost"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class RelocatedMarker:
    marker_id: int
    x: float
    y: float


@dataclass(frozen=True, slots=True)
class RelocalizationResult:
    state: RelocalizationState
    message: str
    reference_to_live: tuple[tuple[float, float, float], ...]
    live_to_reference: tuple[tuple[float, float, float], ...]
    candidate_matches: int
    inliers: int
    inlier_ratio: float
    median_error_pixels: float
    reference_coverage: float


@dataclass(slots=True)
class RelocalizationSnapshot:
    state: RelocalizationState = RelocalizationState.DISABLED
    message: str = "Live calibration relocalization is disabled"
    calibration_path: str = ""
    approval_status: str = ""
    analyzed_sequence: int = 0
    analyzed_frames: int = 0
    analysis_fps: float = 0.0
    inference_ms: float = 0.0
    last_analysis_monotonic: float = 0.0
    candidate_matches: int = 0
    inliers: int = 0
    inlier_ratio: float = 0.0
    median_error_pixels: float = 0.0
    reference_coverage: float = 0.0
    reference_size: tuple[int, int] = (0, 0)
    live_size: tuple[int, int] = (0, 0)
    reference_to_live: tuple[tuple[float, float, float], ...] = (
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
    )
    live_to_reference: tuple[tuple[float, float, float], ...] = (
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
    )
    marker_positions: tuple[RelocatedMarker, ...] = ()
    last_error: str = ""

    @property
    def motion_safe(self) -> bool:
        return self.state == RelocalizationState.LOCKED and self.approval_status in {
            "approved",
            "legacy_approved",
        }

    def copy(self) -> RelocalizationSnapshot:
        return replace(self, marker_positions=tuple(self.marker_positions))


class AudienceRelocalizer:
    """Match curated reference landmarks to the current Audience camera frame."""

    def __init__(
        self,
        reference_bgr: np.ndarray,
        review: CalibrationReviewData,
        *,
        maximum_width: int = 1280,
        maximum_features: int = 8000,
    ) -> None:
        if reference_bgr is None or reference_bgr.size == 0:
            raise ValueError("The Audience calibration reference image is unavailable.")
        if len(review.audience_markers) < 8:
            raise ValueError("At least eight curated markers are required for relocalization.")
        self.reference = _validated_bgr(reference_bgr)
        self.review = review
        self.maximum_width = max(640, min(1920, int(maximum_width)))
        self.maximum_features = max(1000, min(20000, int(maximum_features)))
        self.reference_small, self.reference_scale = _scaled(self.reference, self.maximum_width)
        self.reference_gray = _feature_image(self.reference_small)
        mask = np.zeros(self.reference_gray.shape, dtype=np.uint8)
        radius = max(18, round(min(mask.shape) * 0.028))
        for marker in review.audience_markers:
            point = (
                round(marker.audience_x * self.reference_small.shape[1]),
                round(marker.audience_y * self.reference_small.shape[0]),
            )
            cv2.circle(mask, point, radius, 255, -1, cv2.LINE_AA)
        self.detector = cv2.SIFT_create(
            nfeatures=self.maximum_features,
            contrastThreshold=0.018,
            edgeThreshold=12,
        )
        self.reference_keypoints, self.reference_descriptors = self.detector.detectAndCompute(
            self.reference_gray,
            mask,
        )
        if self.reference_descriptors is None or len(self.reference_keypoints) < 8:
            raise ValueError("Curated markers contain too few repeatable image features.")

    def estimate(self, live_bgr: np.ndarray) -> RelocalizationResult:
        live = _validated_bgr(live_bgr)
        live_small, live_scale = _scaled(live, self.maximum_width)
        live_gray = _feature_image(live_small)
        live_keypoints, live_descriptors = self.detector.detectAndCompute(live_gray, None)
        if live_descriptors is None or len(live_keypoints) < 8:
            return _failed_result("The live Audience image has too few visible features.")
        matches = _mutual_ratio_matches(
            self.reference_descriptors,
            live_descriptors,
            ratio=0.72,
        )
        if len(matches) < 4:
            return _failed_result(
                f"Only {len(matches)} unambiguous curated landmark matches were found."
            )
        source = np.float32([self.reference_keypoints[item.queryIdx].pt for item in matches])
        target = np.float32([live_keypoints[item.trainIdx].pt for item in matches])
        robust_method = getattr(cv2, "USAC_MAGSAC", cv2.RANSAC)
        small_matrix, mask = cv2.findHomography(
            source,
            target,
            robust_method,
            3.0,
            None,
            10000,
            0.999,
        )
        if small_matrix is None or mask is None or not np.isfinite(small_matrix).all():
            return _failed_result("The live Audience transform could not be estimated.")
        inlier_mask = mask.reshape(-1).astype(bool)
        inliers = int(inlier_mask.sum())
        if inliers < 4:
            return _failed_result("Fewer than four landmark matches agreed geometrically.")
        reference_scale_matrix = np.asarray(
            [[self.reference_scale, 0, 0], [0, self.reference_scale, 0], [0, 0, 1]],
            dtype=np.float64,
        )
        live_scale_matrix = np.asarray(
            [[live_scale, 0, 0], [0, live_scale, 0], [0, 0, 1]],
            dtype=np.float64,
        )
        matrix = np.linalg.inv(live_scale_matrix) @ small_matrix @ reference_scale_matrix
        if abs(float(matrix[2, 2])) < 1e-12:
            return _failed_result("The live Audience transform is singular.")
        matrix /= matrix[2, 2]
        try:
            inverse = np.linalg.inv(matrix)
        except np.linalg.LinAlgError:
            return _failed_result("The live Audience transform cannot be inverted.")
        inverse /= inverse[2, 2]
        projected = cv2.perspectiveTransform(
            source[inlier_mask, None, :],
            small_matrix,
        )[:, 0, :]
        errors = np.linalg.norm(projected - target[inlier_mask], axis=1) / max(live_scale, 1e-9)
        median_error = float(np.median(errors))
        ratio = inliers / len(matches)
        coverage = _convex_coverage(
            source[inlier_mask],
            self.reference_small.shape[1],
            self.reference_small.shape[0],
        )
        plausible = _plausible_transform(
            matrix,
            (self.reference.shape[1], self.reference.shape[0]),
            (live.shape[1], live.shape[0]),
        )
        locked = _is_motion_safe_lock(
            inliers=inliers,
            inlier_ratio=ratio,
            median_error_pixels=median_error,
            reference_coverage=coverage,
            plausible=plausible,
        )
        degraded = inliers >= 8 and ratio >= 0.20 and median_error <= 8.0 and plausible
        state = (
            RelocalizationState.LOCKED
            if locked
            else RelocalizationState.DEGRADED
            if degraded
            else RelocalizationState.LOST
        )
        message = (
            f"Locked with {inliers} landmarks at {median_error:.2f}px"
            if state == RelocalizationState.LOCKED
            else f"Calibration confidence is low: {inliers} landmarks at {median_error:.2f}px"
            if state == RelocalizationState.DEGRADED
            else f"Calibration lock lost: {inliers} landmarks at {median_error:.2f}px"
        )
        return RelocalizationResult(
            state=state,
            message=message,
            reference_to_live=_matrix_tuple(matrix),
            live_to_reference=_matrix_tuple(inverse),
            candidate_matches=len(matches),
            inliers=inliers,
            inlier_ratio=ratio,
            median_error_pixels=median_error,
            reference_coverage=coverage,
        )


class AudienceRelocalizationService:
    """Low-rate, latest-frame-only relocalization worker with fail-closed health."""

    def __init__(
        self,
        broker: LatestFrameBroker,
        data_root: Path,
        config: CameraTrackingConfig,
        logger: Any | None = None,
        *,
        initially_active: bool = True,
    ) -> None:
        self.broker = broker
        self.data_root = data_root
        self.config = config
        self.logger = logger
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._reload_event = threading.Event()
        self._activity_event = threading.Event()
        if initially_active:
            self._activity_event.set()
        self._thread: threading.Thread | None = None
        self._snapshot = RelocalizationSnapshot()
        self._review: CalibrationReviewData | None = None
        self._engine: AudienceRelocalizer | None = None
        self._last_sequence = 0
        self._analysis_times: list[float] = []

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def active(self) -> bool:
        return self._activity_event.is_set()

    def set_active(self, active: bool) -> None:
        selected = bool(active) and self.config.relocalization_enabled
        # Adding another activity owner while analysis is already running must
        # not throw away a healthy lock.  Automation previously armed against
        # LOCKED and then immediately reset this snapshot to WAITING, causing
        # its own safety check to disarm before the first camera command.
        if selected == self._activity_event.is_set() and self.running:
            return
        if selected:
            self._activity_event.set()
        else:
            self._activity_event.clear()
        with self._lock:
            if not self.config.relocalization_enabled:
                self._snapshot.state = RelocalizationState.DISABLED
                self._snapshot.message = "Live calibration relocalization is disabled"
            elif selected:
                self._snapshot.state = RelocalizationState.WAITING
                self._snapshot.message = "Waiting for an active calibration frame"
            else:
                self._snapshot.state = RelocalizationState.IDLE
                self._snapshot.message = (
                    "Heavily throttled until calibration or tracking is actively in use"
                )
                self._snapshot.marker_positions = ()
                self._snapshot.analysis_fps = 0.0

    def start(self) -> None:
        if not self.config.relocalization_enabled or self.running:
            return
        self._stop_event.clear()
        self._reload_event.set()
        with self._lock:
            self._snapshot.state = (
                RelocalizationState.WAITING if self.active else RelocalizationState.IDLE
            )
            self._snapshot.message = (
                "Loading the approved Audience calibration"
                if self.active
                else "Heavily throttled until calibration or tracking is actively in use"
            )
        self._thread = threading.Thread(
            target=self._run,
            name="production-hub-audience-relocalization",
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
            self._snapshot = RelocalizationSnapshot()
            self._engine = None
            self._review = None

    def reconfigure(self, config: CameraTrackingConfig) -> None:
        old_signature = (
            self.config.relocalization_enabled,
            self.config.relocalization_fps,
            self.config.relocalization_maximum_width,
            self.config.relocalization_stale_seconds,
        )
        new_signature = (
            config.relocalization_enabled,
            config.relocalization_fps,
            config.relocalization_maximum_width,
            config.relocalization_stale_seconds,
        )
        was_active = self.active
        self.config = config
        if old_signature != new_signature:
            self.stop()
            if config.relocalization_enabled:
                self.start()
                self.set_active(was_active)
            else:
                self.set_active(False)
        elif not config.relocalization_enabled:
            self.set_active(False)

    def reload_calibration(self) -> None:
        self._reload_event.set()

    def snapshot(self) -> RelocalizationSnapshot:
        with self._lock:
            return self._snapshot.copy()

    def stabilized_regions(
        self,
        regions: list[CameraSceneRegion] | tuple[CameraSceneRegion, ...],
    ) -> tuple[CameraSceneRegion, ...]:
        snapshot = self.snapshot()
        with self._lock:
            review = self._review
        if snapshot.state != RelocalizationState.LOCKED or review is None:
            return ()
        # Scene polygons are normalized against the fixed Audience camera.
        # A calibration-version mismatch is still surfaced in the editor for
        # review, but it must not make every stage region disappear at runtime.
        # Projecting them through the current, approved relocalization is the
        # conservative fallback: admission remains restricted to the stage
        # side of the room, and motion still fails closed unless this approved
        # map itself is locked.
        compatible = tuple(regions)
        return transform_scene_regions(
            compatible,
            snapshot.reference_to_live,
            snapshot.reference_size,
            snapshot.live_size,
        )

    def live_point_to_reference(self, x: float, y: float) -> tuple[float, float] | None:
        snapshot = self.snapshot()
        if snapshot.state != RelocalizationState.LOCKED:
            return None
        return _project_normalized(
            snapshot.live_to_reference,
            x,
            y,
            snapshot.live_size,
            snapshot.reference_size,
        )

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if not self.active:
                self._activity_event.wait(timeout=1.0)
                continue
            if self._reload_event.is_set():
                self._reload_event.clear()
                self._load_engine()
            interval = 1.0 / max(0.1, self.config.relocalization_fps)
            packet = self.broker.frame(VideoSourceKey.AUDIENCE)
            now = time.monotonic()
            snapshot = self.snapshot()
            if self._engine is None:
                self._stop_event.wait(0.25)
                continue
            if packet is None or packet.sequence == self._last_sequence:
                if (
                    snapshot.last_analysis_monotonic > 0
                    and now - snapshot.last_analysis_monotonic
                    > max(3.0, self.config.relocalization_stale_seconds)
                ):
                    with self._lock:
                        self._snapshot.state = RelocalizationState.LOST
                        self._snapshot.message = "Audience calibration is stale; motion is blocked"
                        self._snapshot.marker_positions = ()
                self._stop_event.wait(0.05)
                continue
            if now - snapshot.last_analysis_monotonic < interval:
                self._stop_event.wait(0.02)
                continue
            self._analyze(packet.sequence, packet.image)

    def _load_engine(self) -> None:
        try:
            review = load_active_calibration_review(self.data_root)
            if review is None:
                with self._lock:
                    self._engine = None
                    self._review = None
                    self._snapshot = RelocalizationSnapshot(
                        state=RelocalizationState.WAITING,
                        message="No approved Audience calibration is available",
                    )
                return
            reference = cv2.imread(str(review.audience_image_path), cv2.IMREAD_COLOR)
            engine = AudienceRelocalizer(
                reference,
                review,
                maximum_width=self.config.relocalization_maximum_width,
            )
            with self._lock:
                self._review = review
                self._engine = engine
                self._last_sequence = 0
                self._snapshot = RelocalizationSnapshot(
                    state=RelocalizationState.WAITING,
                    message="Waiting for a fresh Audience frame",
                    calibration_path=str(review.map_path),
                    approval_status=review.approval_status,
                    reference_size=review.audience_size,
                )
        except Exception as exc:
            with self._lock:
                self._engine = None
                self._review = None
                self._snapshot = RelocalizationSnapshot(
                    state=RelocalizationState.ERROR,
                    message="Approved calibration could not be loaded",
                    last_error=str(exc),
                )
            self._log("warning", "audience_relocalization_load_failed", str(exc))

    def _analyze(self, sequence: int, image: QImage) -> None:
        engine = self._engine
        review = self._review
        if engine is None or review is None:
            return
        started = time.monotonic()
        try:
            live = qimage_to_bgr(image)
            result = engine.estimate(live)
            markers = _relocated_markers(
                review,
                result.reference_to_live,
                (live.shape[1], live.shape[0]),
            ) if result.state in {RelocalizationState.LOCKED, RelocalizationState.DEGRADED} else ()
            completed = time.monotonic()
            self._analysis_times.append(completed)
            self._analysis_times = self._analysis_times[-30:]
            elapsed = self._analysis_times[-1] - self._analysis_times[0] if len(self._analysis_times) > 1 else 0.0
            rate = (len(self._analysis_times) - 1) / elapsed if elapsed > 0 else 0.0
            with self._lock:
                self._snapshot = RelocalizationSnapshot(
                    state=result.state,
                    message=result.message,
                    calibration_path=str(review.map_path),
                    approval_status=review.approval_status,
                    analyzed_sequence=sequence,
                    analyzed_frames=self._snapshot.analyzed_frames + 1,
                    analysis_fps=rate,
                    inference_ms=(completed - started) * 1000.0,
                    last_analysis_monotonic=completed,
                    candidate_matches=result.candidate_matches,
                    inliers=result.inliers,
                    inlier_ratio=result.inlier_ratio,
                    median_error_pixels=result.median_error_pixels,
                    reference_coverage=result.reference_coverage,
                    reference_size=review.audience_size,
                    live_size=(live.shape[1], live.shape[0]),
                    reference_to_live=result.reference_to_live,
                    live_to_reference=result.live_to_reference,
                    marker_positions=markers,
                )
                self._last_sequence = sequence
        except Exception as exc:
            completed = time.monotonic()
            with self._lock:
                self._last_sequence = sequence
                self._snapshot.state = RelocalizationState.ERROR
                self._snapshot.message = "Audience relocalization failed; motion is blocked"
                self._snapshot.last_error = str(exc)
                self._snapshot.last_analysis_monotonic = completed
                self._snapshot.marker_positions = ()
            self._log("warning", "audience_relocalization_frame_failed", str(exc))

    def _log(self, level: str, event: str, message: str) -> None:
        if self.logger is None:
            return
        callback = getattr(self.logger, level, None)
        if callback is not None:
            callback(event, message)


def qimage_to_bgr(image: QImage) -> np.ndarray:
    if image.isNull():
        raise ValueError("Cannot relocalize a null Audience image.")
    converted = image.convertToFormat(QImage.Format.Format_RGBA8888)
    height = converted.height()
    width = converted.width()
    stride = converted.bytesPerLine()
    raw = np.frombuffer(converted.bits(), dtype=np.uint8, count=stride * height)
    rgba = raw.reshape((height, stride))[:, : width * 4].reshape((height, width, 4)).copy()
    return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)


def _relocated_markers(
    review: CalibrationReviewData,
    matrix: tuple[tuple[float, float, float], ...],
    live_size: tuple[int, int],
) -> tuple[RelocatedMarker, ...]:
    selected: list[RelocatedMarker] = []
    for marker in review.audience_markers:
        point = _project_normalized(
            matrix,
            marker.audience_x,
            marker.audience_y,
            review.audience_size,
            live_size,
        )
        if point is not None and -0.05 <= point[0] <= 1.05 and -0.05 <= point[1] <= 1.05:
            selected.append(RelocatedMarker(marker.marker_id, point[0], point[1]))
    return tuple(selected)


def _project_normalized(
    matrix: tuple[tuple[float, float, float], ...],
    x: float,
    y: float,
    source_size: tuple[int, int],
    target_size: tuple[int, int],
) -> tuple[float, float] | None:
    source_x = float(x) * source_size[0]
    source_y = float(y) * source_size[1]
    selected = np.asarray(matrix, dtype=np.float64)
    projected = selected @ np.asarray([source_x, source_y, 1.0], dtype=np.float64)
    if abs(float(projected[2])) < 1e-9:
        return None
    return (
        float(projected[0] / projected[2]) / max(1, target_size[0]),
        float(projected[1] / projected[2]) / max(1, target_size[1]),
    )


def _failed_result(message: str) -> RelocalizationResult:
    identity = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    return RelocalizationResult(
        RelocalizationState.LOST,
        message,
        identity,
        identity,
        0,
        0,
        0.0,
        float("inf"),
        0.0,
    )


def _mutual_ratio_matches(
    reference_descriptors: np.ndarray,
    live_descriptors: np.ndarray,
    *,
    ratio: float,
) -> list[cv2.DMatch]:
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    forward_pairs = matcher.knnMatch(reference_descriptors, live_descriptors, k=2)
    reverse_pairs = matcher.knnMatch(live_descriptors, reference_descriptors, k=2)
    reverse_best = {
        pair[0].queryIdx: pair[0].trainIdx
        for pair in reverse_pairs
        if len(pair) == 2 and pair[0].distance < ratio * pair[1].distance
    }
    return [
        pair[0]
        for pair in forward_pairs
        if len(pair) == 2
        and pair[0].distance < ratio * pair[1].distance
        and reverse_best.get(pair[0].trainIdx) == pair[0].queryIdx
    ]


def _scaled(image: np.ndarray, maximum_width: int) -> tuple[np.ndarray, float]:
    width = image.shape[1]
    scale = min(1.0, maximum_width / width)
    if scale >= 0.999:
        return image, 1.0
    return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA), scale


def _feature_image(image: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)


def _validated_bgr(image: np.ndarray) -> np.ndarray:
    if not isinstance(image, np.ndarray) or image.size == 0:
        raise ValueError("Audience image is invalid.")
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.ndim == 3 and image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    if image.ndim != 3 or image.shape[2] < 3:
        raise ValueError("Audience image must contain RGB pixels.")
    return image[:, :, :3]


def _convex_coverage(points: np.ndarray, width: int, height: int) -> float:
    if len(points) < 3:
        return 0.0
    hull = cv2.convexHull(np.asarray(points, dtype=np.float32))
    return float(cv2.contourArea(hull)) / max(1.0, float(width * height))


def _is_motion_safe_lock(
    *,
    inliers: int,
    inlier_ratio: float,
    median_error_pixels: float,
    reference_coverage: float,
    plausible: bool,
) -> bool:
    broad_spatial_lock = (
        inliers >= 18
        and inlier_ratio >= 0.32
        and median_error_pixels <= 4.0
        and reference_coverage >= 0.035
    )
    # Fixed architecture in this room produces a very precise cluster of
    # stage landmarks even when it covers slightly less of the full wide
    # image. Accept that case only with substantially stronger inlier, ratio,
    # and error requirements than the general spatial lock.
    high_precision_lock = (
        inliers >= 24
        and inlier_ratio >= 0.75
        and median_error_pixels <= 1.75
        and reference_coverage >= 0.015
    )
    return bool(plausible and (broad_spatial_lock or high_precision_lock))


def _plausible_transform(
    matrix: np.ndarray,
    reference_size: tuple[int, int],
    live_size: tuple[int, int],
) -> bool:
    width, height = reference_size
    corners = np.float32([[[0, 0], [width, 0], [width, height], [0, height]]])
    projected = cv2.perspectiveTransform(corners, matrix)[0]
    if not np.isfinite(projected).all():
        return False
    area = abs(float(cv2.contourArea(projected)))
    live_area = float(live_size[0] * live_size[1])
    ratio = area / max(1.0, live_area)
    center = projected.mean(axis=0)
    return 0.35 <= ratio <= 2.8 and -live_size[0] <= center[0] <= live_size[0] * 2 and -live_size[1] <= center[1] <= live_size[1] * 2


def _matrix_tuple(matrix: np.ndarray) -> tuple[tuple[float, float, float], ...]:
    return tuple(tuple(float(value) for value in row) for row in matrix)
