from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from production_hub.calibration.review import (
    CalibrationReviewData,
    load_active_calibration_review,
)
from production_hub.tracking.models import NormalizedRect


@dataclass(frozen=True, slots=True)
class PtzMotorPose:
    pan: int
    tilt: int
    zoom: int


@dataclass(frozen=True, slots=True)
class CalibrationPoseAnchor:
    name: str
    center_x: float
    center_y: float
    footprint_width: float
    footprint_height: float
    motor: PtzMotorPose
    ptz_size: tuple[int, int]
    audience_size: tuple[int, int]
    ptz_to_audience: tuple[tuple[float, float, float], ...]


class PtzGeometryModel:
    """Bounded Audience-reference to Panasonic motor mapping.

    The camera sweep is deliberately the only source of geometry. Pan and tilt
    use local inverse-distance interpolation between measured poses; zoom uses
    the measured relationship between motor position and projected field of
    view. Targets outside the swept coverage fail closed.
    """

    def __init__(
        self,
        map_path: Path,
        created_at: str,
        anchors: Sequence[CalibrationPoseAnchor],
        *,
        minimum_zoom: int = 0x555,
        maximum_zoom: int = 0xE50,
        coverage_margin: float = 0.10,
    ) -> None:
        if len(anchors) < 4:
            raise ValueError("At least four motor-calibrated PTZ poses are required.")
        self.map_path = Path(map_path)
        self.created_at = str(created_at)
        self.anchors = tuple(anchors)
        self.minimum_zoom = max(0x555, int(minimum_zoom))
        self.maximum_zoom = min(0xFFF, max(self.minimum_zoom, int(maximum_zoom)))
        self.coverage_margin = max(0.0, min(0.25, float(coverage_margin)))
        self._hull = _convex_hull((item.center_x, item.center_y) for item in anchors)
        self._coverage_hull = _convex_hull(
            point
            for item in anchors
            for point in (
                (
                    item.center_x - item.footprint_width / 2.0,
                    item.center_y - item.footprint_height / 2.0,
                ),
                (
                    item.center_x + item.footprint_width / 2.0,
                    item.center_y - item.footprint_height / 2.0,
                ),
                (
                    item.center_x + item.footprint_width / 2.0,
                    item.center_y + item.footprint_height / 2.0,
                ),
                (
                    item.center_x - item.footprint_width / 2.0,
                    item.center_y + item.footprint_height / 2.0,
                ),
            )
        )
        self._pan_plane = self._fit_motor_plane("pan")
        self._tilt_plane = self._fit_motor_plane("tilt")
        self._center_x_pose_plane = self._fit_pose_center_plane("center_x")
        self._center_y_pose_plane = self._fit_pose_center_plane("center_y")
        self._zoom_slope, self._zoom_intercept = self._fit_zoom_model()

    @classmethod
    def load_active(
        cls,
        data_root: Path,
        *,
        minimum_zoom: int = 0x555,
        maximum_zoom: int = 0xE50,
    ) -> PtzGeometryModel:
        review = load_active_calibration_review(data_root)
        if review is None:
            raise ValueError("No approved active Audience-to-PTZ calibration is available.")
        return cls.from_review(
            review,
            minimum_zoom=minimum_zoom,
            maximum_zoom=maximum_zoom,
        )

    @classmethod
    def load_active_panorama(
        cls,
        data_root: Path,
        *,
        minimum_zoom: int = 0x555,
        maximum_zoom: int = 0xE50,
    ) -> PtzGeometryModel:
        """Load motor geometry in canonical reference-PTZ panorama space."""

        review = load_active_calibration_review(data_root)
        if review is None:
            raise ValueError("No approved active PTZ panorama calibration is available.")
        return cls.from_panorama_review(
            review,
            minimum_zoom=minimum_zoom,
            maximum_zoom=maximum_zoom,
        )

    @classmethod
    def from_review(
        cls,
        review: CalibrationReviewData,
        *,
        minimum_zoom: int = 0x555,
        maximum_zoom: int = 0xE50,
    ) -> PtzGeometryModel:
        payload = _load_json(review.map_path)
        reference_payload = _load_json(review.reference_calibration_path)
        reference_alignment = reference_payload.get("alignment", {})
        reference_ptz_size = reference_alignment.get("ptz_size", (1920, 1080))
        reference_audience_size = reference_alignment.get(
            "audience_size",
            review.audience_size,
        )
        anchors: list[CalibrationPoseAnchor] = []
        for item in payload.get("poses", ()):
            if item.get("status") != "accepted":
                continue
            motor = item.get("motor_position", {})
            if not all(key in motor for key in ("pan", "tilt", "zoom")):
                continue
            link = item.get("ptz_link", {})
            ptz_size = _size(
                item.get("ptz_size")
                or link.get("ptz_size")
                or reference_ptz_size
            )
            audience_size = _size(
                item.get("audience_size")
                or link.get("audience_size")
                or reference_audience_size
            )
            matrix = _matrix(
                item.get("ptz_to_audience") or link.get("ptz_to_audience")
            )
            corners = tuple(
                _project_normalized(
                    matrix,
                    x,
                    y,
                    audience_size,
                )
                for x, y in (
                    (0.0, 0.0),
                    (float(ptz_size[0]), 0.0),
                    (float(ptz_size[0]), float(ptz_size[1])),
                    (0.0, float(ptz_size[1])),
                )
            )
            center = _project_normalized(
                matrix,
                ptz_size[0] / 2.0,
                ptz_size[1] / 2.0,
                audience_size,
            )
            xs = [point[0] for point in corners]
            ys = [point[1] for point in corners]
            anchors.append(
                CalibrationPoseAnchor(
                    name=str(item.get("name", f"Pose {len(anchors) + 1}")),
                    center_x=center[0],
                    center_y=center[1],
                    footprint_width=max(xs) - min(xs),
                    footprint_height=max(ys) - min(ys),
                    motor=PtzMotorPose(
                        pan=int(motor["pan"]),
                        tilt=int(motor["tilt"]),
                        zoom=int(motor["zoom"]),
                    ),
                    ptz_size=ptz_size,
                    audience_size=audience_size,
                    ptz_to_audience=matrix,
                )
            )
        return cls(
            review.map_path,
            review.created_at,
            anchors,
            minimum_zoom=minimum_zoom,
            maximum_zoom=maximum_zoom,
        )

    @classmethod
    def from_panorama_review(
        cls,
        review: CalibrationReviewData,
        *,
        minimum_zoom: int = 0x555,
        maximum_zoom: int = 0xE50,
    ) -> PtzGeometryModel:
        """Build click geometry without crossing camera viewpoints.

        Each sweep image is projected back into the saved reference PTZ image.
        Pan, tilt, zoom, viewport highlighting, and Click-to-Frame therefore all
        share one PTZ-native coordinate system. Audience-camera parallax is
        handled separately by the presentation warp.
        """

        payload = _load_json(review.map_path)
        reference_payload = _load_json(review.reference_calibration_path)
        alignment = reference_payload.get("alignment", {})
        reference_ptz_size = _size(alignment.get("ptz_size", (1920, 1080)))
        anchors: list[CalibrationPoseAnchor] = []
        for item in payload.get("poses", ()):
            if not isinstance(item, dict) or item.get("status") != "accepted":
                continue
            motor = item.get("motor_position", {})
            if not all(key in motor for key in ("pan", "tilt", "zoom")):
                continue
            link = item.get("ptz_link") or {}
            ptz_size = _size(
                item.get("ptz_size")
                or link.get("ptz_size")
                or reference_ptz_size
            )
            try:
                pose_to_reference = np.linalg.inv(_reference_to_pose_matrix(item))
            except (KeyError, TypeError, ValueError, np.linalg.LinAlgError):
                continue
            if not np.isfinite(pose_to_reference).all():
                continue
            matrix = tuple(
                tuple(float(value) for value in row)
                for row in pose_to_reference
            )
            corners = tuple(
                _project_normalized(
                    matrix,
                    x,
                    y,
                    reference_ptz_size,
                )
                for x, y in (
                    (0.0, 0.0),
                    (float(ptz_size[0]), 0.0),
                    (float(ptz_size[0]), float(ptz_size[1])),
                    (0.0, float(ptz_size[1])),
                )
            )
            center = _project_normalized(
                matrix,
                ptz_size[0] / 2.0,
                ptz_size[1] / 2.0,
                reference_ptz_size,
            )
            xs = [point[0] for point in corners]
            ys = [point[1] for point in corners]
            anchors.append(
                CalibrationPoseAnchor(
                    name=str(item.get("name", f"Pose {len(anchors) + 1}")),
                    center_x=center[0],
                    center_y=center[1],
                    footprint_width=max(xs) - min(xs),
                    footprint_height=max(ys) - min(ys),
                    motor=PtzMotorPose(
                        pan=int(motor["pan"]),
                        tilt=int(motor["tilt"]),
                        zoom=int(motor["zoom"]),
                    ),
                    ptz_size=ptz_size,
                    audience_size=reference_ptz_size,
                    ptz_to_audience=matrix,
                )
            )
        return cls(
            review.map_path,
            review.created_at,
            anchors,
            minimum_zoom=minimum_zoom,
            maximum_zoom=maximum_zoom,
        )

    @property
    def calibrated_zoom_range(self) -> tuple[int, int]:
        values = [item.motor.zoom for item in self.anchors]
        return min(values), max(values)

    @property
    def motor_bounds(self) -> tuple[tuple[int, int], tuple[int, int]]:
        pans = [item.motor.pan for item in self.anchors]
        tilts = [item.motor.tilt for item in self.anchors]
        return (min(pans), max(pans)), (min(tilts), max(tilts))

    def contains_reference_point(self, x: float, y: float) -> bool:
        point = (float(x), float(y))
        if _point_in_convex_polygon(self._coverage_hull, point):
            return True
        return min(
            _point_to_segment_distance(
                point,
                self._coverage_hull[index],
                self._coverage_hull[(index + 1) % len(self._coverage_hull)],
            )
            for index in range(len(self._coverage_hull))
        ) <= self.coverage_margin

    def pose_for_target(
        self,
        target: NormalizedRect,
        *,
        desired_frame_height: float,
    ) -> PtzMotorPose:
        bounds = target
        center_x, center_y = bounds.center
        if (
            bounds.area <= 0
            or not all(
                math.isfinite(value)
                for value in (bounds.x, bounds.y, bounds.width, bounds.height)
            )
        ):
            raise ValueError("The framing target has no area.")
        if not self.contains_reference_point(center_x, center_y):
            raise ValueError("The target is outside the calibrated PTZ sweep coverage.")
        if _point_in_convex_polygon(self._hull, (center_x, center_y)):
            pan = self._interpolate(center_x, center_y, "pan")
            tilt = self._interpolate(center_x, center_y, "tilt")
        else:
            pan = self._motor_plane_value(self._pan_plane, center_x, center_y)
            tilt = self._motor_plane_value(self._tilt_plane, center_x, center_y)
        required_footprint = max(
            bounds.height / max(0.10, float(desired_frame_height)),
            bounds.width / max(0.10, float(desired_frame_height) * 16.0 / 9.0),
        )
        zoom = self._zoom_for_footprint(required_footprint)
        pan_bounds, tilt_bounds = self._extended_motor_bounds()
        return PtzMotorPose(
            pan=max(pan_bounds[0], min(pan_bounds[1], round(pan))),
            tilt=max(tilt_bounds[0], min(tilt_bounds[1], round(tilt))),
            zoom=max(self.minimum_zoom, min(self.maximum_zoom, round(zoom))),
        )

    def ptz_rect_to_reference(
        self,
        bounds: NormalizedRect,
        current_pose: PtzMotorPose,
    ) -> NormalizedRect:
        """Project a PTZ detection through the estimated current viewport."""

        anchor, center_x, center_y, scale = self._pose_projection(current_pose)
        points = []
        for x, y in (
            (bounds.x, bounds.y),
            (bounds.x + bounds.width, bounds.y),
            (bounds.x + bounds.width, bounds.y + bounds.height),
            (bounds.x, bounds.y + bounds.height),
        ):
            projected = _project_normalized(
                anchor.ptz_to_audience,
                x * anchor.ptz_size[0],
                y * anchor.ptz_size[1],
                anchor.audience_size,
            )
            points.append(
                (
                    center_x + (projected[0] - anchor.center_x) * scale,
                    center_y + (projected[1] - anchor.center_y) * scale,
                )
            )
        return _bounds(points)

    def reference_polygon_for_pose(
        self,
        current_pose: PtzMotorPose,
    ) -> tuple[tuple[float, float], ...]:
        """Estimate the current PTZ image footprint in Audience-reference space."""

        anchor, center_x, center_y, scale = self._pose_projection(current_pose)
        corners = tuple(
            _project_normalized(
                anchor.ptz_to_audience,
                x,
                y,
                anchor.audience_size,
            )
            for x, y in (
                (0.0, 0.0),
                (float(anchor.ptz_size[0]), 0.0),
                (float(anchor.ptz_size[0]), float(anchor.ptz_size[1])),
                (0.0, float(anchor.ptz_size[1])),
            )
        )
        return tuple(
            (
                center_x + (x - anchor.center_x) * scale,
                center_y + (y - anchor.center_y) * scale,
            )
            for x, y in corners
        )

    def _pose_projection(
        self,
        current_pose: PtzMotorPose,
    ) -> tuple[CalibrationPoseAnchor, float, float, float]:
        anchor = min(
            self.anchors,
            key=lambda item: (
                ((item.motor.pan - current_pose.pan) / 2048.0) ** 2
                + ((item.motor.tilt - current_pose.tilt) / 1024.0) ** 2
                + ((item.motor.zoom - current_pose.zoom) / 1024.0) ** 2
            ),
        )
        center_x = self._pose_plane_value(
            self._center_x_pose_plane,
            current_pose.pan,
            current_pose.tilt,
        )
        center_y = self._pose_plane_value(
            self._center_y_pose_plane,
            current_pose.pan,
            current_pose.tilt,
        )
        predicted_height = math.exp(
            self._zoom_slope * current_pose.zoom + self._zoom_intercept
        )
        scale = max(
            0.20,
            min(
                5.0,
                predicted_height / max(0.02, anchor.footprint_height),
            ),
        )
        return anchor, center_x, center_y, scale

    def _interpolate(self, x: float, y: float, component: str) -> float:
        nearest = sorted(
            self.anchors,
            key=lambda item: (item.center_x - x) ** 2 + (item.center_y - y) ** 2,
        )[: min(6, len(self.anchors))]
        distances = [math.hypot(item.center_x - x, item.center_y - y) for item in nearest]
        if distances[0] <= 1e-6:
            return float(getattr(nearest[0].motor, component))
        weights = [1.0 / max(1e-6, distance) ** 2 for distance in distances]
        return sum(
            weight * float(getattr(item.motor, component))
            for item, weight in zip(nearest, weights, strict=True)
        ) / sum(weights)

    def _fit_motor_plane(self, component: str) -> tuple[float, float, float]:
        design = np.asarray(
            [(item.center_x, item.center_y, 1.0) for item in self.anchors],
            dtype=np.float64,
        )
        values = np.asarray(
            [float(getattr(item.motor, component)) for item in self.anchors],
            dtype=np.float64,
        )
        coefficients, _residuals, rank, _singular = np.linalg.lstsq(
            design,
            values,
            rcond=None,
        )
        if rank < 3 or not np.isfinite(coefficients).all():
            raise ValueError("The calibration motor grid cannot support edge extrapolation.")
        return tuple(float(value) for value in coefficients)

    def _fit_pose_center_plane(self, component: str) -> tuple[float, float, float]:
        pan_center = float(np.mean([item.motor.pan for item in self.anchors]))
        tilt_center = float(np.mean([item.motor.tilt for item in self.anchors]))
        pan_scale = max(
            1.0,
            float(np.ptp([item.motor.pan for item in self.anchors])),
        )
        tilt_scale = max(
            1.0,
            float(np.ptp([item.motor.tilt for item in self.anchors])),
        )
        design = np.asarray(
            [
                (
                    (item.motor.pan - pan_center) / pan_scale,
                    (item.motor.tilt - tilt_center) / tilt_scale,
                    1.0,
                )
                for item in self.anchors
            ],
            dtype=np.float64,
        )
        values = np.asarray(
            [float(getattr(item, component)) for item in self.anchors],
            dtype=np.float64,
        )
        coefficients, _residuals, rank, _singular = np.linalg.lstsq(
            design,
            values,
            rcond=None,
        )
        if rank < 3 or not np.isfinite(coefficients).all():
            raise ValueError("The calibration motor grid cannot locate the current PTZ view.")
        return (
            float(coefficients[0] / pan_scale),
            float(coefficients[1] / tilt_scale),
            float(
                coefficients[2]
                - coefficients[0] * pan_center / pan_scale
                - coefficients[1] * tilt_center / tilt_scale
            ),
        )

    @staticmethod
    def _motor_plane_value(
        coefficients: tuple[float, float, float],
        x: float,
        y: float,
    ) -> float:
        return coefficients[0] * x + coefficients[1] * y + coefficients[2]

    @staticmethod
    def _pose_plane_value(
        coefficients: tuple[float, float, float],
        pan: int,
        tilt: int,
    ) -> float:
        return coefficients[0] * pan + coefficients[1] * tilt + coefficients[2]

    def _extended_motor_bounds(self) -> tuple[tuple[int, int], tuple[int, int]]:
        pan_bounds, tilt_bounds = self.motor_bounds

        def extend(bounds: tuple[int, int]) -> tuple[int, int]:
            span = max(1, bounds[1] - bounds[0])
            allowance = max(128, round(span * 0.60))
            return max(0, bounds[0] - allowance), min(0xFFFF, bounds[1] + allowance)

        return extend(pan_bounds), extend(tilt_bounds)

    def _fit_zoom_model(self) -> tuple[float, float]:
        grouped: dict[int, list[float]] = {}
        for item in self.anchors:
            if item.footprint_height > 0:
                grouped.setdefault(item.motor.zoom, []).append(item.footprint_height)
        samples = sorted(
            (zoom, float(np.median(values))) for zoom, values in grouped.items()
        )
        if len(samples) < 2:
            # Conservative fallback: do not extrapolate beyond the measured zoom.
            return -1e-6, math.log(max(0.05, samples[0][1])) + samples[0][0] * 1e-6
        zooms = np.asarray([item[0] for item in samples], dtype=np.float64)
        logs = np.log(np.asarray([max(0.02, item[1]) for item in samples]))
        slope, intercept = np.polyfit(zooms, logs, 1)
        if not math.isfinite(float(slope)) or float(slope) >= -1e-8:
            raise ValueError("The calibration zoom samples are not physically consistent.")
        return float(slope), float(intercept)

    def _zoom_for_footprint(self, footprint_height: float) -> float:
        required = max(0.02, min(1.5, float(footprint_height)))
        return (math.log(required) - self._zoom_intercept) / self._zoom_slope


def transform_live_rect_to_reference(
    bounds: NormalizedRect,
    live_to_reference: Sequence[Sequence[float]],
    live_size: tuple[int, int],
    reference_size: tuple[int, int],
) -> NormalizedRect:
    matrix = _matrix(live_to_reference)
    if min(*live_size, *reference_size) <= 0:
        raise ValueError("Relocalization image sizes must be positive.")
    points = []
    for x, y in (
        (bounds.x, bounds.y),
        (bounds.x + bounds.width, bounds.y),
        (bounds.x + bounds.width, bounds.y + bounds.height),
        (bounds.x, bounds.y + bounds.height),
    ):
        projected = _project(
            matrix,
            x * live_size[0],
            y * live_size[1],
        )
        points.append(
            (projected[0] / reference_size[0], projected[1] / reference_size[1])
        )
    return _bounds(points)


def transform_reference_rect_to_live(
    bounds: NormalizedRect,
    reference_to_live: Sequence[Sequence[float]],
    reference_size: tuple[int, int],
    live_size: tuple[int, int],
) -> NormalizedRect:
    matrix = _matrix(reference_to_live)
    if min(*live_size, *reference_size) <= 0:
        raise ValueError("Relocalization image sizes must be positive.")
    points = []
    for x, y in (
        (bounds.x, bounds.y),
        (bounds.x + bounds.width, bounds.y),
        (bounds.x + bounds.width, bounds.y + bounds.height),
        (bounds.x, bounds.y + bounds.height),
    ):
        projected = _project(
            matrix,
            x * reference_size[0],
            y * reference_size[1],
        )
        points.append((projected[0] / live_size[0], projected[1] / live_size[1]))
    return _bounds(points)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Calibration map must be a JSON object.")
    return value


def _reference_to_pose_matrix(pose_payload: dict[str, Any]) -> np.ndarray:
    direct = pose_payload.get("reference_ptz_to_pose")
    if direct is not None:
        return np.asarray(_matrix(direct), dtype=np.float64)
    link = pose_payload.get("ptz_link") or {}
    linked = link.get("audience_to_ptz")
    if linked is not None:
        return np.asarray(_matrix(linked), dtype=np.float64)
    if int(pose_payload.get("index", 0)) == 1:
        return np.eye(3, dtype=np.float64)
    raise KeyError("PTZ pose has no reference-image transform")


def _size(value: Any) -> tuple[int, int]:
    width, height = value
    selected = round(float(width)), round(float(height))
    if min(selected) <= 0:
        raise ValueError("Calibration image size must be positive.")
    return selected


def _matrix(value: Any) -> tuple[tuple[float, float, float], ...]:
    rows = tuple(tuple(float(item) for item in row) for row in value)
    if len(rows) != 3 or any(len(row) != 3 for row in rows):
        raise ValueError("Calibration transform must be 3x3.")
    if not np.isfinite(np.asarray(rows)).all():
        raise ValueError("Calibration transform must be finite.")
    return rows


def _project(
    matrix: Sequence[Sequence[float]],
    x: float,
    y: float,
) -> tuple[float, float]:
    denominator = matrix[2][0] * x + matrix[2][1] * y + matrix[2][2]
    if abs(denominator) < 1e-9:
        raise ValueError("Calibration projection is singular.")
    return (
        (matrix[0][0] * x + matrix[0][1] * y + matrix[0][2]) / denominator,
        (matrix[1][0] * x + matrix[1][1] * y + matrix[1][2]) / denominator,
    )


def _project_normalized(
    matrix: Sequence[Sequence[float]],
    x: float,
    y: float,
    output_size: tuple[int, int],
) -> tuple[float, float]:
    projected = _project(matrix, x, y)
    return projected[0] / output_size[0], projected[1] / output_size[1]


def _bounds(points: Sequence[tuple[float, float]]) -> NormalizedRect:
    xs = [item[0] for item in points]
    ys = [item[1] for item in points]
    return NormalizedRect(
        min(xs),
        min(ys),
        max(xs) - min(xs),
        max(ys) - min(ys),
    ).clamped()


def _convex_hull(points: Iterable[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    unique = sorted(set(points))
    if len(unique) <= 2:
        return tuple(unique)

    def cross(
        origin: tuple[float, float],
        first: tuple[float, float],
        second: tuple[float, float],
    ) -> float:
        return (first[0] - origin[0]) * (second[1] - origin[1]) - (
            first[1] - origin[1]
        ) * (second[0] - origin[0])

    lower: list[tuple[float, float]] = []
    for point in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    upper: list[tuple[float, float]] = []
    for point in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return tuple(lower[:-1] + upper[:-1])


def _point_in_convex_polygon(
    polygon: Sequence[tuple[float, float]],
    point: tuple[float, float],
) -> bool:
    if len(polygon) < 3:
        return False
    signs = []
    for index, first in enumerate(polygon):
        second = polygon[(index + 1) % len(polygon)]
        cross = (second[0] - first[0]) * (point[1] - first[1]) - (
            second[1] - first[1]
        ) * (point[0] - first[0])
        if abs(cross) > 1e-10:
            signs.append(cross > 0)
    return not signs or all(value == signs[0] for value in signs)


def _point_to_segment_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    delta_x = end[0] - start[0]
    delta_y = end[1] - start[1]
    length_squared = delta_x * delta_x + delta_y * delta_y
    if length_squared <= 1e-12:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    projection = (
        (point[0] - start[0]) * delta_x
        + (point[1] - start[1]) * delta_y
    ) / length_squared
    selected = max(0.0, min(1.0, projection))
    closest = start[0] + selected * delta_x, start[1] + selected * delta_y
    return math.hypot(point[0] - closest[0], point[1] - closest[1])
