from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha1
from typing import Callable, Sequence

import cv2
import numpy as np


ProgressCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class StructuralPlaneSettings:
    """Bounded settings for finding repeated planar surfaces across camera views."""

    maximum_width: int = 1600
    maximum_features: int = 12000
    ratio_threshold: float = 0.76
    ransac_reprojection_threshold: float = 2.5
    maximum_models_per_pose: int = 10
    minimum_model_inliers: int = 14
    minimum_component_points: int = 7
    component_radius_fraction: float = 0.085
    minimum_area_fraction: float = 0.0015
    maximum_area_fraction: float = 0.40
    minimum_pose_confirmations: int = 2
    merge_iou_threshold: float = 0.16
    merge_containment_threshold: float = 0.55

    def __post_init__(self) -> None:
        object.__setattr__(self, "maximum_width", max(640, min(3840, int(self.maximum_width))))
        object.__setattr__(self, "maximum_features", max(1000, min(30000, int(self.maximum_features))))
        object.__setattr__(self, "ratio_threshold", max(0.5, min(0.95, float(self.ratio_threshold))))
        object.__setattr__(
            self,
            "ransac_reprojection_threshold",
            max(0.5, min(10.0, float(self.ransac_reprojection_threshold))),
        )
        object.__setattr__(self, "maximum_models_per_pose", max(1, min(30, int(self.maximum_models_per_pose))))
        object.__setattr__(self, "minimum_model_inliers", max(8, int(self.minimum_model_inliers)))
        object.__setattr__(self, "minimum_component_points", max(4, int(self.minimum_component_points)))
        object.__setattr__(
            self,
            "component_radius_fraction",
            max(0.01, min(0.30, float(self.component_radius_fraction))),
        )
        object.__setattr__(
            self,
            "minimum_area_fraction",
            max(0.0001, min(0.25, float(self.minimum_area_fraction))),
        )
        object.__setattr__(
            self,
            "maximum_area_fraction",
            max(self.minimum_area_fraction, min(1.0, float(self.maximum_area_fraction))),
        )
        object.__setattr__(self, "minimum_pose_confirmations", max(1, int(self.minimum_pose_confirmations)))


@dataclass(frozen=True, slots=True)
class StructuralPlaneInput:
    pose_index: int
    pose_name: str
    audience_bgr: np.ndarray
    ptz_bgr: np.ndarray
    audience_to_reference: Sequence[Sequence[float]]


@dataclass(frozen=True, slots=True)
class StructuralPlaneObservation:
    pose_index: int
    pose_name: str
    polygon: tuple[tuple[float, float], ...]
    support_points: int
    median_error_pixels: float
    area_fraction: float


@dataclass(frozen=True, slots=True)
class StructuralPlaneCandidate:
    id: str
    name: str
    polygon: tuple[tuple[float, float], ...]
    supporting_poses: tuple[str, ...]
    observation_count: int
    support_points: int
    median_error_pixels: float
    area_fraction: float
    confidence: float
    color: str
    observations: tuple[StructuralPlaneObservation, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["polygon"] = [list(point) for point in self.polygon]
        payload["supporting_poses"] = list(self.supporting_poses)
        payload["observations"] = [
            {
                **asdict(item),
                "polygon": [list(point) for point in item.polygon],
            }
            for item in self.observations
        ]
        return payload


@dataclass(frozen=True, slots=True)
class StructuralPlaneExtraction:
    planes: tuple[StructuralPlaneCandidate, ...]
    pose_summaries: tuple[dict[str, object], ...]
    total_observations: int


_COLORS = (
    "#25d0c8",
    "#ffb020",
    "#d467ff",
    "#5b8def",
    "#22c55e",
    "#ef5da8",
    "#f97316",
    "#8b5cf6",
    "#06b6d4",
    "#eab308",
    "#84cc16",
    "#f43f5e",
)


def extract_structural_planes(
    reference_audience_bgr: np.ndarray,
    inputs: Sequence[StructuralPlaneInput],
    settings: StructuralPlaneSettings | None = None,
    *,
    progress: ProgressCallback | None = None,
) -> StructuralPlaneExtraction:
    """Extract repeated planar feature supports in Audience-reference coordinates.

    Points and homographies are deliberately intermediate evidence. The public result
    contains reviewable polygons, and a polygon must normally recur in multiple PTZ
    poses before it is retained.
    """

    selected = settings or StructuralPlaneSettings()
    reference = _validated_bgr(reference_audience_bgr, "Audience reference")
    reference_size = (reference.shape[1], reference.shape[0])
    observations: list[StructuralPlaneObservation] = []
    summaries: list[dict[str, object]] = []
    for item in inputs:
        if progress is not None:
            progress(f"Analyzing pose {item.pose_index}: {item.pose_name}")
        try:
            pose_observations, summary = _extract_pose_observations(
                item,
                reference_size,
                selected,
            )
            observations.extend(pose_observations)
            summaries.append(summary)
        except (ValueError, cv2.error, np.linalg.LinAlgError) as exc:
            summaries.append(
                {
                    "pose_index": item.pose_index,
                    "pose_name": item.pose_name,
                    "status": "failed",
                    "reason": str(exc),
                    "observations": 0,
                }
            )
            if progress is not None:
                progress(f"Pose {item.pose_index} skipped: {exc}")

    candidates = _merge_observations(observations, selected)
    if progress is not None:
        progress(
            f"Retained {len(candidates)} repeated structural plane candidate(s) "
            f"from {len(observations)} observations."
        )
    return StructuralPlaneExtraction(
        planes=tuple(candidates),
        pose_summaries=tuple(summaries),
        total_observations=len(observations),
    )


def render_structural_plane_overlay(
    reference_audience_bgr: np.ndarray,
    planes: Sequence[StructuralPlaneCandidate],
) -> np.ndarray:
    image = _validated_bgr(reference_audience_bgr, "Audience reference").copy()
    overlay = image.copy()
    height, width = image.shape[:2]
    for plane in planes:
        polygon = np.asarray(
            [[round(x * width), round(y * height)] for x, y in plane.polygon],
            dtype=np.int32,
        )
        if len(polygon) < 3:
            continue
        color = _bgr(plane.color)
        cv2.fillPoly(overlay, [polygon], color, cv2.LINE_AA)
    cv2.addWeighted(overlay, 0.22, image, 0.78, 0.0, image)
    for index, plane in enumerate(planes, start=1):
        polygon = np.asarray(
            [[round(x * width), round(y * height)] for x, y in plane.polygon],
            dtype=np.int32,
        )
        if len(polygon) < 3:
            continue
        color = _bgr(plane.color)
        cv2.polylines(image, [polygon], True, color, 3, cv2.LINE_AA)
        center = polygon.astype(np.float32).mean(axis=0).astype(int)
        label = f"P{index:02d} {plane.observation_count} views"
        (text_width, text_height), _ = cv2.getTextSize(
            label,
            cv2.FONT_HERSHEY_SIMPLEX,
            0.58,
            2,
        )
        x = int(max(0, min(width - text_width - 10, center[0] - text_width // 2)))
        y = int(max(text_height + 8, min(height - 4, center[1])))
        cv2.rectangle(image, (x - 4, y - text_height - 6), (x + text_width + 5, y + 5), (9, 11, 15), -1)
        cv2.putText(
            image,
            label,
            (x, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.58,
            color,
            2,
            cv2.LINE_AA,
        )
    return image


def _extract_pose_observations(
    item: StructuralPlaneInput,
    reference_size: tuple[int, int],
    settings: StructuralPlaneSettings,
) -> tuple[list[StructuralPlaneObservation], dict[str, object]]:
    audience = _validated_bgr(item.audience_bgr, "Audience pose")
    ptz = _validated_bgr(item.ptz_bgr, "PTZ pose")
    audience_small, audience_scale = _scaled(audience, settings.maximum_width)
    ptz_small, ptz_scale = _scaled(ptz, settings.maximum_width)
    detector = cv2.SIFT_create(
        nfeatures=settings.maximum_features,
        contrastThreshold=0.018,
        edgeThreshold=12,
    )
    audience_points, audience_descriptors = detector.detectAndCompute(
        _feature_image(audience_small),
        None,
    )
    ptz_points, ptz_descriptors = detector.detectAndCompute(
        _feature_image(ptz_small),
        None,
    )
    if audience_descriptors is None or ptz_descriptors is None:
        raise ValueError("one or both images have no usable SIFT features")
    matches = _mutual_ratio_matches(
        audience_descriptors,
        ptz_descriptors,
        settings.ratio_threshold,
    )
    if len(matches) < settings.minimum_model_inliers:
        raise ValueError(f"only {len(matches)} reciprocal feature matches were found")
    source = np.float32([audience_points[match.queryIdx].pt for match in matches])
    target = np.float32([ptz_points[match.trainIdx].pt for match in matches])
    remaining = np.arange(len(matches), dtype=np.int32)
    transform = _matrix(item.audience_to_reference)
    observations: list[StructuralPlaneObservation] = []
    fitted_models = 0
    while (
        fitted_models < settings.maximum_models_per_pose
        and len(remaining) >= settings.minimum_model_inliers
    ):
        fitted_models += 1
        cv2.setRNGSeed(2901 + item.pose_index * 97 + fitted_models)
        model, mask = cv2.findHomography(
            source[remaining],
            target[remaining],
            getattr(cv2, "USAC_MAGSAC", cv2.RANSAC),
            settings.ransac_reprojection_threshold,
            None,
            12000,
            0.999,
        )
        if model is None or mask is None or not np.isfinite(model).all():
            break
        local_inliers = mask.reshape(-1).astype(bool)
        inlier_indices = remaining[local_inliers]
        if len(inlier_indices) < settings.minimum_model_inliers:
            break
        model /= model[2, 2]
        inverse = np.linalg.inv(model)
        forward = cv2.perspectiveTransform(source[inlier_indices, None, :], model)[:, 0, :]
        backward = cv2.perspectiveTransform(target[inlier_indices, None, :], inverse)[:, 0, :]
        errors = (
            np.linalg.norm(forward - target[inlier_indices], axis=1) / max(ptz_scale, 1e-9)
            + np.linalg.norm(backward - source[inlier_indices], axis=1) / max(audience_scale, 1e-9)
        ) / 2.0
        radius = settings.component_radius_fraction * np.hypot(
            audience_small.shape[1],
            audience_small.shape[0],
        )
        for component in _spatial_components(source[inlier_indices], radius):
            if len(component) < settings.minimum_component_points:
                continue
            selected_indices = inlier_indices[component]
            pose_pixels = source[selected_indices] / max(audience_scale, 1e-9)
            reference_pixels = cv2.perspectiveTransform(
                pose_pixels[:, None, :].astype(np.float32),
                transform,
            )[:, 0, :]
            polygon = _support_polygon(reference_pixels, reference_size)
            if len(polygon) < 3:
                continue
            area = _normalized_polygon_area(polygon)
            if not settings.minimum_area_fraction <= area <= settings.maximum_area_fraction:
                continue
            component_errors = errors[component]
            observations.append(
                StructuralPlaneObservation(
                    pose_index=item.pose_index,
                    pose_name=item.pose_name,
                    polygon=polygon,
                    support_points=len(component),
                    median_error_pixels=float(np.median(component_errors)),
                    area_fraction=area,
                )
            )
        remaining = remaining[~local_inliers]
    return observations, {
        "pose_index": item.pose_index,
        "pose_name": item.pose_name,
        "status": "accepted",
        "audience_keypoints": len(audience_points),
        "ptz_keypoints": len(ptz_points),
        "reciprocal_matches": len(matches),
        "models": fitted_models,
        "observations": len(observations),
    }


def _merge_observations(
    observations: Sequence[StructuralPlaneObservation],
    settings: StructuralPlaneSettings,
) -> list[StructuralPlaneCandidate]:
    clusters: list[list[StructuralPlaneObservation]] = []
    ordered = sorted(
        observations,
        key=lambda item: (item.support_points * max(item.area_fraction, 0.001)),
        reverse=True,
    )
    for observation in ordered:
        best_index = -1
        best_score = 0.0
        for index, cluster in enumerate(clusters):
            if any(item.pose_index == observation.pose_index for item in cluster):
                continue
            representative = _representative_polygon(cluster)
            iou, containment = _polygon_overlap(observation.polygon, representative)
            if iou >= settings.merge_iou_threshold or containment >= settings.merge_containment_threshold:
                score = iou + containment * 0.35
                if score > best_score:
                    best_index = index
                    best_score = score
        if best_index >= 0:
            clusters[best_index].append(observation)
        else:
            clusters.append([observation])

    accepted: list[tuple[tuple[tuple[float, float], ...], list[StructuralPlaneObservation]]] = []
    for cluster in clusters:
        poses = {item.pose_index for item in cluster}
        if len(poses) < settings.minimum_pose_confirmations:
            continue
        polygon = _representative_polygon(cluster)
        area = _normalized_polygon_area(polygon)
        if settings.minimum_area_fraction <= area <= settings.maximum_area_fraction:
            accepted.append((polygon, cluster))
    accepted.sort(
        key=lambda item: (
            len({observation.pose_index for observation in item[1]}),
            sum(observation.support_points for observation in item[1]),
            _normalized_polygon_area(item[0]),
        ),
        reverse=True,
    )
    deduplicated: list[
        tuple[tuple[tuple[float, float], ...], list[StructuralPlaneObservation]]
    ] = []
    for polygon, cluster in accepted:
        area = _normalized_polygon_area(polygon)
        duplicate = False
        for existing_polygon, _existing_cluster in deduplicated:
            existing_area = _normalized_polygon_area(existing_polygon)
            iou, containment = _polygon_overlap(polygon, existing_polygon)
            similar_scale = min(area, existing_area) / max(area, existing_area, 1e-9) >= 0.50
            if similar_scale and (iou >= 0.48 or containment >= 0.82):
                duplicate = True
                break
        if not duplicate:
            deduplicated.append((polygon, cluster))

    result: list[StructuralPlaneCandidate] = []
    for index, (polygon, cluster) in enumerate(deduplicated, start=1):
        support_points = sum(item.support_points for item in cluster)
        pose_names = tuple(dict.fromkeys(item.pose_name for item in cluster))
        median_error = float(np.median([item.median_error_pixels for item in cluster]))
        area = _normalized_polygon_area(polygon)
        confirmations = len({item.pose_index for item in cluster})
        confidence = min(
            1.0,
            0.45 * min(1.0, confirmations / 4.0)
            + 0.35 * min(1.0, support_points / 80.0)
            + 0.20 * max(0.0, 1.0 - median_error / 5.0),
        )
        fingerprint = ";".join(f"{x:.4f},{y:.4f}" for x, y in polygon)
        identifier = sha1(fingerprint.encode("utf-8")).hexdigest()[:10]
        result.append(
            StructuralPlaneCandidate(
                id=f"generated-structural-plane-{identifier}",
                name=f"Structural Plane {index:02d}",
                polygon=polygon,
                supporting_poses=pose_names,
                observation_count=confirmations,
                support_points=support_points,
                median_error_pixels=median_error,
                area_fraction=area,
                confidence=round(confidence, 4),
                color=_COLORS[(index - 1) % len(_COLORS)],
                observations=tuple(cluster),
            )
        )
    return result


def _representative_polygon(
    observations: Sequence[StructuralPlaneObservation],
) -> tuple[tuple[float, float], ...]:
    if len(observations) == 1:
        return observations[0].polygon
    # Use a weighted medoid instead of the union. A union grows across partially
    # overlapping zoomed views and can swallow several real surfaces; the medoid is
    # the single observed support that agrees best with the other camera poses.
    best = observations[0]
    best_score = -1.0
    for candidate in observations:
        overlap_score = 0.0
        for other in observations:
            if candidate is other:
                continue
            iou, containment = _polygon_overlap(candidate.polygon, other.polygon)
            overlap_score += iou + 0.30 * containment
        quality = min(1.0, candidate.support_points / 25.0) * 0.12
        error = min(1.0, candidate.median_error_pixels / 5.0) * 0.08
        score = overlap_score + quality - error
        if score > best_score:
            best = candidate
            best_score = score
    return best.polygon


def _support_polygon(
    points: np.ndarray,
    reference_size: tuple[int, int],
) -> tuple[tuple[float, float], ...]:
    finite = points[np.isfinite(points).all(axis=1)]
    if len(finite) < 3:
        return ()
    normalized = finite / np.asarray(reference_size, dtype=np.float32)
    normalized = normalized[
        (normalized[:, 0] >= -0.03)
        & (normalized[:, 0] <= 1.03)
        & (normalized[:, 1] >= -0.03)
        & (normalized[:, 1] <= 1.03)
    ]
    if len(normalized) < 3:
        return ()
    hull = cv2.convexHull(normalized.astype(np.float32)).reshape(-1, 2)
    if len(hull) < 3:
        return ()
    # A modest expansion turns sparse evidence into a reviewable surface patch while
    # staying close to the cross-camera support. It is intentionally conservative.
    center = hull.mean(axis=0)
    hull = center + (hull - center) * 1.08
    perimeter = cv2.arcLength(hull[:, None, :], True)
    simplified = cv2.approxPolyDP(hull[:, None, :], max(0.002, perimeter * 0.025), True).reshape(-1, 2)
    if len(simplified) < 3:
        return ()
    return tuple((float(np.clip(x, 0.0, 1.0)), float(np.clip(y, 0.0, 1.0))) for x, y in simplified)


def _spatial_components(points: np.ndarray, radius: float) -> list[np.ndarray]:
    if len(points) == 0:
        return []
    radius_squared = float(radius * radius)
    unvisited = set(range(len(points)))
    components: list[np.ndarray] = []
    while unvisited:
        start = unvisited.pop()
        queue = [start]
        component = [start]
        while queue:
            current = queue.pop()
            remaining = np.fromiter(unvisited, dtype=np.int32)
            if len(remaining) == 0:
                continue
            distances = np.sum((points[remaining] - points[current]) ** 2, axis=1)
            neighbors = remaining[distances <= radius_squared]
            for neighbor in neighbors.tolist():
                if neighbor in unvisited:
                    unvisited.remove(neighbor)
                    queue.append(neighbor)
                    component.append(neighbor)
        components.append(np.asarray(component, dtype=np.int32))
    return components


def _polygon_overlap(
    first: Sequence[Sequence[float]],
    second: Sequence[Sequence[float]],
) -> tuple[float, float]:
    first_contour = cv2.convexHull(np.asarray(first, dtype=np.float32)).reshape(-1, 2)
    second_contour = cv2.convexHull(np.asarray(second, dtype=np.float32)).reshape(-1, 2)
    first_area = abs(float(cv2.contourArea(first_contour)))
    second_area = abs(float(cv2.contourArea(second_contour)))
    if min(first_area, second_area) <= 1e-9:
        return 0.0, 0.0
    intersection, _ = cv2.intersectConvexConvex(first_contour, second_contour)
    intersection = max(0.0, float(intersection))
    union = first_area + second_area - intersection
    return (
        intersection / union if union > 1e-9 else 0.0,
        intersection / min(first_area, second_area),
    )


def _normalized_polygon_area(polygon: Sequence[Sequence[float]]) -> float:
    return abs(float(cv2.contourArea(np.asarray(polygon, dtype=np.float32))))


def _mutual_ratio_matches(
    source_descriptors: np.ndarray,
    target_descriptors: np.ndarray,
    ratio: float,
) -> list[cv2.DMatch]:
    matcher = cv2.BFMatcher(cv2.NORM_L2, crossCheck=False)
    forward = _ratio_matches(matcher.knnMatch(source_descriptors, target_descriptors, k=2), ratio)
    reverse = _ratio_matches(matcher.knnMatch(target_descriptors, source_descriptors, k=2), ratio)
    reverse_pairs = {(item.trainIdx, item.queryIdx) for item in reverse}
    return sorted(
        (item for item in forward if (item.queryIdx, item.trainIdx) in reverse_pairs),
        key=lambda item: item.distance,
    )


def _ratio_matches(neighbors: Sequence[Sequence[cv2.DMatch]], ratio: float) -> list[cv2.DMatch]:
    return [
        options[0]
        for options in neighbors
        if len(options) >= 2 and options[0].distance < ratio * options[1].distance
    ]


def _validated_bgr(image: np.ndarray, label: str) -> np.ndarray:
    if not isinstance(image, np.ndarray) or image.size == 0 or image.ndim not in {2, 3}:
        raise ValueError(f"{label} is empty or invalid")
    if image.dtype != np.uint8:
        image = np.clip(image, 0, 255).astype(np.uint8)
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    if image.shape[2] != 3:
        raise ValueError(f"{label} must have one, three, or four channels")
    return image


def _scaled(image: np.ndarray, maximum_width: int) -> tuple[np.ndarray, float]:
    scale = min(1.0, maximum_width / image.shape[1])
    if scale >= 1.0:
        return image, 1.0
    return (
        cv2.resize(
            image,
            (maximum_width, max(1, round(image.shape[0] * scale))),
            interpolation=cv2.INTER_AREA,
        ),
        scale,
    )


def _feature_image(image: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)


def _matrix(value: Sequence[Sequence[float]]) -> np.ndarray:
    matrix = np.asarray(value, dtype=np.float64)
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        raise ValueError("Audience-to-reference transform must be a finite 3x3 matrix")
    if abs(float(np.linalg.det(matrix))) < 1e-12:
        raise ValueError("Audience-to-reference transform is singular")
    return matrix


def _bgr(hex_color: str) -> tuple[int, int, int]:
    value = str(hex_color).lstrip("#")
    if len(value) != 6:
        return (255, 92, 124)
    red, green, blue = (int(value[index : index + 2], 16) for index in (0, 2, 4))
    return blue, green, red
